"""The tools Duma's model can call.

Two kinds. "Direct" tools (`buscar_atleta`, `resumen_atleta`, `buscar_atletas`,
`grafica`) send what they find to the chat as text, a file built by `render` or
a chart drawn by `charts`, and the model gets back only a short acknowledgement
with no names in it. `cifras`, `catalogo` and `consultar` answer the model:
with totals, which identify nobody; with the names of groups and events; and
with one row per person where the name is a code. `entrenos_atleta` and
`vueltas_entreno`, `plan_atleta`, `pagos_atleta`, `perfil_atleta`,
`comprobantes`, `errores_garmin` and `avisos_enviados` do both: the chat gets a
table drawn by `charts` (or a CSV, an album of tables, or a short card), with
names on it, and the model gets the same figures with no name at all, or with a
code in its place. Either way the model never reads a person's name it was not
given by the admin.

The tools that propose (`revisar_comprobante`, `rechazar_comprobante`,
`proponer_aviso`, `guardar_preferencia`) change nothing: they store what would
run and show it with buttons. The click runs it, in `duma.main`.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import unicodedata
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Awaitable, Optional
from zoneinfo import ZoneInfo

from duma import charts, confirmations
from duma.api_client import ApiError, MuunganoApi
from duma.confirmations import Confirmations
from duma.preferences import PreferenceError, Preferences, clean
from duma.pseudonyms import Pseudonyms
from duma.render import RECEIPT_STATUS, _day, _day_year, _extras, _gap, _goal_seconds, _money, _num, _period, _race_time, matches_csv, render_candidates, render_matches, render_summary, table_csv

log = logging.getLogger(__name__)


@dataclass
class OutFile:
    name: str
    content: bytes
    # Shown in the chat as a picture instead of attached as a download.
    photo: bool = False
    mime: str = "image/png"


@dataclass
class ToolResult:
    # Written to the chat as is. None when the tool has nothing to show.
    direct_text: Optional[str]
    # What the model sees. Never contains athlete names.
    to_model: str
    is_error: bool = False
    # Attached to the chat with `direct_text` as its caption; several pictures go as one album. The model never sees them.
    files: list[OutFile] = field(default_factory=list)
    # (label, callback data) pairs shown under `direct_text`: a proposal waiting for an admin's click.
    buttons: Optional[list[tuple[str, str]]] = None


FILTERS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "maxItems": 8,
    "items": {
        "type": "object",
        "properties": {
            "tipo": {
                "type": "string",
                "enum": ["evento", "pago", "grupo", "entrenos", "membresia", "comprobante", "perfil", "faltas"],
                "description": (
                    "`membresia`: por lo que cubre su membresía (`situacion`). `comprobante`: tiene un comprobante "
                    "en ese `situacion`. `perfil`: por `nivel`, `sede`, `genero` o `reloj`. `faltas`: dejó sin "
                    "hacer al menos `minimo` entrenos prescritos entre `desde` y `hasta`."
                ),
            },
            "situacion": {
                "type": "string",
                "enum": ["vigente", "vencida", "sin_membresia", "vence", "pendiente", "aprobado", "rechazado"],
                "description": (
                    "Para `membresia`: `vigente` (cubre hoy), `vencida`, `sin_membresia` (nunca tuvo) o `vence` "
                    "(su último día cubierto cae entre `desde` y `hasta`). Para `comprobante`: `pendiente`, "
                    "`aprobado` o `rechazado`; con `desde` y `hasta` opcionales, que son la fecha en que se subió."
                ),
            },
            "beneficio": {"type": "boolean", "description": "Solo `comprobante`: solo los de beneficio (true) o solo los de pago (false)."},
            "nivel": {"type": "integer", "description": "Solo `perfil`: nivel del atleta."},
            "sede": {"type": "string", "description": "Solo `perfil`: parte del nombre de la sede."},
            "genero": {"type": "string", "enum": ["femenino", "masculino"], "description": "Solo `perfil`."},
            "reloj": {"type": "boolean", "description": "Solo `perfil`: con reloj vinculado (true) o sin él (false)."},
            "nombre": {
                "type": "string",
                "description": "Para `evento` y `grupo`: parte del nombre, mínimo 2 letras.",
            },
            "otros": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 10,
                "description": (
                    "Solo `grupo`: más nombres de grupo, para juntar varios en un mismo conjunto («los de MTY y los de "
                    "Berlin»). Dos filtros `grupo` separados no sirven: nadie está en dos grupos."
                ),
            },
            "condicion": {
                "type": "string",
                "enum": ["inscritos", "ya_paso", "con_tiempo"],
                "description": (
                    "Solo `evento`. `inscritos` (por defecto) es cualquier inscripción; `ya_paso`, "
                    "inscritos en un evento que ya ocurrió; `con_tiempo`, solo quienes tienen "
                    "resultado registrado, que son pocos."
                ),
            },
            "anio": {"type": "integer", "description": "Solo `evento`: año de la edición."},
            "desde": {"type": "string", "description": "Para `pago`, `entrenos`, `faltas`, `membresia` que vence y `comprobante`: YYYY-MM-DD."},
            "hasta": {"type": "string", "description": "Igual que `desde`: YYYY-MM-DD."},
            "minimo": {"type": "integer", "description": "`entrenos`: mínimo de entrenos hechos. `faltas`: mínimo sin hacer (por defecto 1)."},
        },
        "required": ["tipo"],
    },
}

SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "buscar_atleta",
        "description": (
            "Busca personas del equipo por nombre (atletas, coaches y admins) y muestra la lista en el chat. Úsala "
            "cuando el administrador quiera saber quién es alguien o cuántos se llaman así, sin pedir su resumen. "
            "Con `filtros` busca el nombre solo entre quienes los cumplen (\"Ari, la de Berlin\"): si son pocos te "
            "devuelve sus códigos a ti, sin mandar nada al chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "texto": {"type": "string", "description": "Parte del nombre o apellido, mínimo 2 letras."},
                "filtros": FILTERS_SCHEMA,
                "estado": {
                    "type": "string",
                    "enum": ["todos", "activos", "inactivos"],
                    "description": (
                        "Por defecto `todos` (activos y pausados). Usa `activos` o `inactivos` solo si el "
                        "administrador pide únicamente unos u otros."
                    ),
                },
            },
            "required": ["texto"],
        },
    },
    {
        "name": "resumen_atleta",
        "description": (
            "Muestra en el chat el resumen de un atleta: entrenos hechos contra prescritos, score, ritmo y frecuencia "
            "cardiaca promedio, su entreno más largo y, si tiene evento principal, su ciclo y la semana en que va. "
            "Si el nombre es ambiguo, el administrador recibe la lista de candidatos y te contesta cuál; entonces "
            "vuelve a llamarla con `grupo` o el nombre completo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre y/o apellido del atleta."},
                "grupo": {"type": "string", "description": "Nombre del grupo, para distinguir homónimos."},
                "desde": {"type": "string", "description": "Fecha inicial YYYY-MM-DD. Por defecto, 30 días atrás."},
                "hasta": {"type": "string", "description": "Fecha final YYYY-MM-DD. Por defecto, hoy."},
                "ciclo": {
                    "type": "boolean",
                    "description": "Cubrir desde el inicio de su ciclo de entrenamiento hasta hoy (o la carrera).",
                },
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "entrenos_atleta",
        "description": (
            "Manda al chat los entrenos que hizo un atleta, como tabla en imagen o como CSV según `formato`, y te devuelve a ti las mismas "
            "filas: número del entreno, fecha, tipo, km, duración, ritmo, frecuencia cardiaca, score y cuántas "
            "vueltas tiene. Úsala cuando pregunten por un "
            "entreno en particular («su tirada de 32 km», «qué hizo el martes», «cuáles entrenos estás contando») o "
            "cuando necesites sus ritmos y distancias para razonar o estimar algo. Con `km_min` y `km_max` buscas "
            "por distancia: para «el de 32 km» pide de 30 a 34. No trae los entrenos no hechos ni el título que "
            "puso el coach. Tope: 60 entrenos; si hay más, quedan los más largos. El nombre se resuelve igual que "
            "en `resumen_atleta`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre y/o apellido del atleta."},
                "grupo": {"type": "string", "description": "Nombre del grupo, para distinguir homónimos."},
                "desde": {"type": "string", "description": "Fecha inicial YYYY-MM-DD. Por defecto, 30 días atrás."},
                "hasta": {"type": "string", "description": "Fecha final YYYY-MM-DD. Por defecto, hoy."},
                "ciclo": {
                    "type": "boolean",
                    "description": "Cubrir desde el inicio de su ciclo de entrenamiento hasta hoy (o la carrera).",
                },
                "km_min": {"type": "number", "description": "Solo entrenos de al menos estos km."},
                "km_max": {"type": "number", "description": "Solo entrenos de a lo más estos km."},
                "formato": {
                    "type": "string",
                    "enum": ["auto", "imagen", "csv"],
                    "description": (
                        "Cómo sale al chat. `auto` (por defecto): tabla en imagen hasta 40 filas, CSV si son más. "
                        "`imagen` solo si el administrador pidió imagen: lo que no cabe en una se reparte en varias. "
                        "`csv` solo si pidió archivo."
                    ),
                },
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "vueltas_entreno",
        "description": (
            "Manda al chat el desglose por vuelta (lap) de un entreno, como tabla en imagen o como CSV según `formato`, y te devuelve a ti las "
            "mismas filas: distancia, duración, ritmo, frecuencia cardiaca y score de cada una. Una vuelta sin score es una que la prescripción no califica, "
            "como una recuperación. `entreno` es el número que trae la fila de `entrenos_atleta`: llama primero a "
            "esa."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "El mismo nombre que usaste en `entrenos_atleta`."},
                "grupo": {"type": "string", "description": "Nombre del grupo, para distinguir homónimos."},
                "entreno": {"type": "integer", "description": "Número del entreno, de la fila de `entrenos_atleta`."},
                "formato": {
                    "type": "string",
                    "enum": ["auto", "imagen", "csv"],
                    "description": (
                        "Cómo sale al chat. `auto` (por defecto): tabla en imagen hasta 40 filas, CSV si son más. "
                        "`imagen` solo si el administrador pidió imagen: lo que no cabe en una se reparte en varias. "
                        "`csv` solo si pidió archivo."
                    ),
                },
            },
            "required": ["nombre", "entreno"],
        },
    },
    {
        "name": "plan_atleta",
        "description": (
            "Manda al chat el plan de un atleta día por día, y te devuelve a ti las mismas filas: qué le tocaba o le "
            "toca, si lo hizo, no lo hizo o está por hacer. Sirve para «qué le toca esta semana», «cuáles entrenos "
            "no hizo» o «qué tiene mañana»; admite fechas futuras. Por defecto, una semana atrás y una adelante; "
            "máximo 92 días. No trae el título ni la descripción que escribió el coach."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre y/o apellido del atleta, o su código ATLETA_NN."},
                "grupo": {"type": "string", "description": "Nombre del grupo, para distinguir homónimos."},
                "desde": {"type": "string", "description": "Fecha inicial YYYY-MM-DD."},
                "hasta": {"type": "string", "description": "Fecha final YYYY-MM-DD; puede ser futura."},
                "formato": {
                    "type": "string",
                    "enum": ["auto", "imagen", "csv"],
                    "description": "Igual que en `entrenos_atleta`: `imagen` o `csv` solo si el administrador lo pidió.",
                },
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "pagos_atleta",
        "description": (
            "Manda al chat hasta cuándo está cubierta la membresía de un atleta y sus comprobantes (fecha, estado, "
            "meses, monto, si fue beneficio), y te devuelve lo mismo. Úsala para «¿ya pagó?», «¿cuándo se le "
            "vence?» o «¿qué pasó con su comprobante?»."
        ),
        "input_schema": {"type": "object", "properties": {
                "nombre": {"type": "string", "description": "Nombre y/o apellido del atleta, o su código ATLETA_NN."},
                "grupo": {"type": "string", "description": "Nombre del grupo, para distinguir homónimos."},
        }, "required": ["nombre"]},
    },
    {
        "name": "perfil_atleta",
        "description": (
            "Manda al chat la ficha de un atleta: nivel, sede, género, edad aproximada, reloj y marca, meta, grupo, "
            "membresía y su evento principal. A ti te devuelve lo mismo salvo la meta, que es texto que escribió "
            "el atleta. No hay datos de contacto: ni correo ni teléfono."
        ),
        "input_schema": {"type": "object", "properties": {
                "nombre": {"type": "string", "description": "Nombre y/o apellido del atleta, o su código ATLETA_NN."},
                "grupo": {"type": "string", "description": "Nombre del grupo, para distinguir homónimos."},
        }, "required": ["nombre"]},
    },
    {
        "name": "comprobantes",
        "description": (
            "Manda al chat la lista de comprobantes en un estado (por defecto, los pendientes de revisar) con quién "
            "subió cada uno, y te devuelve las mismas filas con códigos en vez de nombres. `desde` y `hasta` son la "
            "fecha en que se subieron. Para «¿quién tiene comprobante pendiente?», «¿cuántos beneficios hay por "
            "aprobar?» o «¿a quién se le rechazó?»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "situacion": {"type": "string", "enum": ["pendiente", "aprobado", "rechazado"]},
                "beneficio": {"type": "boolean", "description": "Solo los de beneficio (true) o solo los de pago (false)."},
                "desde": {"type": "string", "description": "YYYY-MM-DD."},
                "hasta": {"type": "string", "description": "YYYY-MM-DD."},
                "formato": {
                    "type": "string",
                    "enum": ["auto", "imagen", "csv"],
                    "description": "Igual que en `entrenos_atleta`: `imagen` o `csv` solo si el administrador lo pidió.",
                },
            },
        },
    },
    {
        "name": "buscar_atletas",
        "description": (
            "Muestra en el chat la lista de personas que cumplen TODOS los filtros a la vez: inscritos en un evento, "
            "con un pago aprobado en un periodo, de un grupo, o con un mínimo de entrenos hechos. Sin filtros lista a "
            "todo el equipo. Tú no ves la lista: recibes cuántos son y qué eventos o grupos entendió el API. Si un "
            "nombre no coincidió con nada, díselo al administrador."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filtros": FILTERS_SCHEMA,
                "estado": {
                    "type": "string",
                    "enum": ["todos", "activos", "inactivos"],
                    "description": "Por defecto `todos` (activos y pausados).",
                },
                "solo_contar": {
                    "type": "boolean",
                    "description": "Solo el número, sin mostrar la lista. Para preguntas de «cuántos».",
                },
            },
        },
    },
    {
        "name": "cifras",
        "description": (
            "Totales sobre las personas que cumplen los filtros: cuántas son, pagos aprobados (cuántos y cuánto suman), "
            "entrenos prescritos y hechos, kilómetros y score promedio. Te devuelve solo números, sin nombres, y nada "
            "sale al chat: la respuesta la escribes tú con esas cifras, tal cual y con su periodo. Toda métrica que no "
            "sea `personas` necesita `desde` y `hasta`. `entrenos`, `km` y `score` aceptan como máximo 60 personas y 92 "
            "días; `pagos`, 366 días. Si el API pide acotar, díselo al administrador."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metricas": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["personas", "pagos", "entrenos", "km", "score"]},
                    "minItems": 1,
                },
                "filtros": FILTERS_SCHEMA,
                "estado": {
                    "type": "string",
                    "enum": ["todos", "activos", "inactivos"],
                    "description": "Por defecto `todos` (activos y pausados).",
                },
                "desde": {"type": "string", "description": "Inicio del periodo, YYYY-MM-DD."},
                "hasta": {"type": "string", "description": "Fin del periodo, YYYY-MM-DD."},
            },
            "required": ["metricas"],
        },
    },
    {
        "name": "consultar",
        "description": (
            "Te devuelve, solo a ti, una fila por persona que cumple los filtros, para cuando el administrador pide "
            "analizar, comparar o interpretar («quién va más flojo», «qué tendencia ves», «resúmelo»). Las personas "
            "vienen como códigos ATLETA_NN, sin nombres: úsalos tal cual en tu respuesta, que el sistema los cambia "
            "por los nombres antes de mostrarla. Cada fila trae rol, grupo, estado y, según los filtros, sus eventos "
            "y su último pago. Con `desde` y `hasta` trae además sus entrenos hechos y prescritos, score, km, ritmo y "
            "frecuencia cardiaca del periodo. Para solo listar personas usa `buscar_atletas`; para solo totales, "
            "`cifras`. Tope: 60 personas, o 25 si pides el periodo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filtros": FILTERS_SCHEMA,
                "estado": {
                    "type": "string",
                    "enum": ["todos", "activos", "inactivos"],
                    "description": "Por defecto `todos` (activos y pausados).",
                },
                "desde": {"type": "string", "description": "Inicio del periodo de entrenos, YYYY-MM-DD."},
                "hasta": {"type": "string", "description": "Fin del periodo de entrenos, YYYY-MM-DD."},
            },
        },
    },
    {
        "name": "catalogo",
        "description": (
            "Te devuelve, solo a ti, los nombres reales de todos los grupos (con cuántos miembros tiene cada uno) y de "
            "los eventos más recientes (con su fecha). Nada sale al chat y no trae datos de nadie. Úsala ANTES de "
            "filtrar por un grupo o un evento cuyo nombre exacto no hayas visto ya en esta conversación, para saber a "
            "cuál o cuáles se refiere el administrador. Los filtros comparan letras: «maratón» no encuentra «42k MTY "
            "3:45+»."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "preguntar",
        "description": (
            "Hace una pregunta al administrador con un botón por opción, para que conteste con un toque en vez de "
            "escribir. Úsala siempre que tengas que preguntar entre opciones concretas (cuál grupo, cuál evento, cuál "
            "periodo, cuál métrica). Cada opción es el texto del botón: corto y que se entienda solo, con el nombre "
            "real («42k MTY, los 5 grupos», «Berlin 4:00hr»). Incluye «Todos» si aplica. Después de llamarla no hagas "
            "nada más en este turno: la opción elegida te llega como el siguiente mensaje del administrador. Para "
            "una pregunta abierta, sin opciones, escríbela como texto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pregunta": {"type": "string", "description": "La pregunta, en una línea."},
                "opciones": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 2,
                    "maxItems": 8,
                    "description": "De 2 a 8 opciones, de hasta 40 caracteres cada una.",
                },
            },
            "required": ["pregunta", "opciones"],
        },
    },
    {
        "name": "grafica",
        "description": (
            "Muestra en el chat una gráfica. Tres tipos. `semanal` (por defecto): por semana, de lunes a domingo, "
            "entrenos hechos contra prescritos, kilómetros o score, de UN atleta (con `nombre`) o del conjunto de "
            "personas que cumplen `filtros`; no las dos cosas. `ranking`: las personas del conjunto ordenadas por la "
            "métrica en el periodo, con nombre; si son más de 10, las 5 primeras y las 5 últimas. Para «quién va "
            "mejor», «quién va más flojo», «top 5». `dispersion`: por semana, el score más bajo, la mediana y el más "
            "alto entre las personas del conjunto; dice si el grupo va parejo. Solo con `metrica` score. `ranking` y "
            "`dispersion` son de un conjunto: van con `filtros`, nunca con `nombre`. Sin `desde` y `hasta` cubre las "
            "últimas 8 semanas; el máximo son 92 días y, por filtros, 60 personas. Tú no ves la imagen. De un atleta "
            "y del ranking recibes solo el acuse, sin nombres ni cifras de nadie; de un conjunto por semana, además, "
            "los totales, que puedes comentar. Si el nombre es ambiguo, el administrador recibe los candidatos y te "
            "contesta cuál."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metrica": {"type": "string", "enum": ["entrenos", "km", "score"]},
                "tipo": {
                    "type": "string",
                    "enum": ["semanal", "ranking", "dispersion"],
                    "description": "Por defecto `semanal`.",
                },
                "nombre": {"type": "string", "description": "Nombre y/o apellido, para la gráfica de un atleta."},
                "grupo": {"type": "string", "description": "Con `nombre`: grupo del atleta, para distinguir homónimos."},
                "filtros": FILTERS_SCHEMA,
                "estado": {
                    "type": "string",
                    "enum": ["todos", "activos", "inactivos"],
                    "description": "Con `filtros`. Por defecto `todos` (activos y pausados).",
                },
                "desde": {"type": "string", "description": "Inicio del periodo, YYYY-MM-DD."},
                "hasta": {"type": "string", "description": "Fin del periodo, YYYY-MM-DD."},
            },
            "required": ["metrica"],
        },
    },
    {
        "name": "errores_garmin",
        "description": (
            "Entrenos que no llegaron al reloj porque Garmin los rechazó: manda al chat la tabla (fecha, atleta, tipo, "
            "si se sigue reintentando e intentos; el error completo solo va en el CSV) y te devuelve el total, cuántos atletas, los motivos agrupados y las filas con "
            "códigos en vez de nombres. `desde` y `hasta` son la fecha del entreno; sin ellas, de hace una semana a "
            "dos semanas adelante. No incluye a quien no tiene reloj vinculado: a esos no se les intenta publicar "
            "(búscalos con el filtro de perfil `reloj: false`)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "desde": {"type": "string", "description": "YYYY-MM-DD."},
                "hasta": {"type": "string", "description": "YYYY-MM-DD."},
                "formato": {
                    "type": "string",
                    "enum": ["auto", "imagen", "csv"],
                    "description": "Igual que en `entrenos_atleta`: `imagen` o `csv` solo si el administrador lo pidió.",
                },
            },
        },
    },
    {
        "name": "solicitudes",
        "description": (
            "Solicitudes de ingreso que nadie ha aceptado: manda al chat la tabla (fecha, nombre, ciudad y si ya "
            "llenó el cuestionario) y te devuelve las filas con códigos en vez de nombres. `situacion`: `pendiente` "
            "(por defecto, esperan respuesta), `espera` (lista de espera) o `rechazada`. Para decidir una, "
            "`revisar_solicitud`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "situacion": {"type": "string", "enum": ["pendiente", "espera", "rechazada"]},
                "formato": {
                    "type": "string",
                    "enum": ["auto", "imagen", "csv"],
                    "description": "Igual que en `entrenos_atleta`: `imagen` o `csv` solo si el administrador lo pidió.",
                },
            },
        },
    },
    {
        "name": "avisos_enviados",
        "description": (
            "Avisos que se mandaron por correo o push desde la consola o desde aquí: manda al chat la tabla con fecha, "
            "canal, asunto, destinatarios y cuántos aceptó el proveedor, y te devuelve las mismas cifras sin el asunto "
            "(es texto que escribió una persona). «Aceptado» es que Apple, Google o el servidor de correo lo "
            "recibieron, no que alguien lo leyó. No incluye el resumen semanal ni la cuenta regresiva de eventos, "
            "que son automáticos. Sin fechas, los últimos 30 días."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "desde": {"type": "string", "description": "YYYY-MM-DD."},
                "hasta": {"type": "string", "description": "YYYY-MM-DD."},
                "formato": {
                    "type": "string",
                    "enum": ["auto", "imagen", "csv"],
                    "description": "Igual que en `entrenos_atleta`: `imagen` o `csv` solo si el administrador lo pidió.",
                },
            },
        },
    },
]

WHO = {
    "nombre": {"type": "string", "description": "Nombre y/o apellido del atleta, o su código ATLETA_NN."},
    "grupo": {"type": "string", "description": "Nombre del grupo, para distinguir homónimos."},
}

MEMBER_WRITE_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "renovar_membresia",
        "description": (
            "Propone registrar un pago recibido fuera de la app (efectivo, transferencia directa) y activar la "
            "membresía: lo que la consola llama «Renovar membresía». Úsala también cuando digan «activa la "
            "membresía de…» o «pagó en efectivo». NO la renueva: muestra al administrador el plan, su precio, el "
            "monto recibido y hasta cuándo quedaría cubierto, con los botones Renovar y Cancelar. Necesitas que te "
            "digan los meses (1, 3 o 6); si no lo dijeron, pregunta. El precio lo pone el servidor: `monto` es lo "
            "que el administrador dice que recibió, no lo calcules tú. Si el atleta tiene un comprobante en "
            "revisión, no se puede: ese se aprueba con `revisar_comprobante`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **WHO,
                "meses": {"type": "integer", "enum": [1, 3, 6]},
                "monto": {"type": "number", "description": "Lo que se recibió, en pesos. Solo si el administrador lo dijo."},
                "fecha_pago": {"type": "string", "description": "YYYY-MM-DD. Sin ella, hoy."},
                "referencia": {"type": "string", "description": "Como lo dijo el administrador: «efectivo», una clave de rastreo…"},
            },
            "required": ["nombre", "meses"],
        },
    },
    {
        "name": "pausar_atleta",
        "description": (
            "Propone pausar a un atleta (deja de poder entrar a la app) o reactivarlo. NO lo hace: muestra la "
            "propuesta con los botones de confirmar y Cancelar. No sirve para archivar ni para traer de vuelta a "
            "alguien archivado: eso es en la consola."
        ),
        "input_schema": {
            "type": "object",
            "properties": {**WHO, "accion": {"type": "string", "enum": ["pausar", "reactivar"]}},
            "required": ["nombre", "accion"],
        },
    },
    {
        "name": "tiempo_carrera",
        "description": (
            "Propone registrar el tiempo final de un atleta en una carrera a la que está inscrito. NO lo registra: "
            "muestra el tiempo, el evento, su objetivo y el tiempo que ya tuviera, con los botones Registrar y "
            "Cancelar. `evento` es el nombre (o parte) del evento; si coincide con varios o con ninguno, la "
            "herramienta te dice en cuáles está inscrito y tú preguntas. `tiempo` va como H:MM:SS, tal como te lo "
            "dieron: no lo redondees ni lo estimes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                **WHO,
                "evento": {"type": "string", "description": "Nombre o parte del nombre del evento."},
                "tiempo": {"type": "string", "description": "Tiempo final, H:MM:SS. Ejemplo: 3:42:10."},
            },
            "required": ["nombre", "evento", "tiempo"],
        },
    },
]

APPLICATION_SCHEMA: dict[str, Any] = {
    "name": "revisar_solicitud",
    "description": (
        "Manda al chat la tarjeta de una solicitud de ingreso (quién es, de dónde, cuándo la mandó, su comentario y "
        "si ya llenó el cuestionario) con los botones Aceptar, Lista de espera y Rechazar. Tú no decides: lo hace el "
        "administrador con los botones, y a la persona se le avisa por correo. Sin argumentos manda la pendiente "
        "más antigua con el cuestionario contestado («la siguiente»); con `nombre`, la de esa persona. Manda una por llamada. Sin cuestionario no "
        "se puede aceptar: la tarjeta sale sin ese botón. El grupo y el nivel se asignan después, en la consola."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "nombre": {"type": "string", "description": "Nombre y/o apellido de quien solicitó, o su código ATLETA_NN."},
        },
    },
}

ANNOUNCEMENT_SCHEMA: dict[str, Any] = {
    "name": "proponer_aviso",
    "description": (
        "Propone mandar un aviso por correo, push o ambos. NO lo manda: muestra al administrador el texto completo, "
        "a cuántos atletas llega, por qué canal, y la lista como archivo, con los botones Enviar y Cancelar. Solo "
        "sale si pulsan Enviar; después de llamarla no digas que se envió. Un aviso enviado no se puede retirar.\n"
        "La audiencia siempre son atletas activos y hay que decirla de una de tres formas: `todos: true` (solo si el "
        "administrador dijo expresamente que es para todos; si no lo dijo, pregúntale), `filtros` (los mismos de "
        "`buscar_atletas`: grupo, evento, membresía, perfil, faltas…) y/o `atletas` (códigos ATLETA_NN de personas "
        "que ya buscaste con `buscar_atleta`). `filtros` y `atletas` se suman. Si un grupo no coincide con ninguno, "
        "la herramienta te lo dice: pregunta con `preguntar` cuál es, no adivines.\n"
        "`asunto` y `mensaje` son lo que el administrador te dictó: corrige ortografía si acaso, no agregues datos "
        "ni promesas que no dijo. Si no dijo el canal, pregúntale."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "asunto": {"type": "string", "description": "Título del aviso, máximo 45 caracteres."},
            "mensaje": {"type": "string", "description": "El texto del aviso, tal como lo recibirán."},
            "canal": {"type": "string", "enum": ["correo", "push", "ambos"]},
            "todos": {"type": "boolean", "description": "true = todos los atletas activos. Va solo, sin filtros."},
            "filtros": FILTERS_SCHEMA,
            "atletas": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Códigos ATLETA_NN de personas concretas.",
            },
            "categoria": {
                "type": "string",
                "description": "Categoría del aviso, si el administrador nombró una. Sin ella va la general.",
            },
        },
        "required": ["asunto", "mensaje", "canal"],
    },
}

RECEIPT_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "revisar_comprobante",
        "description": (
            "Manda al chat la foto de un comprobante pendiente con su tarjeta (quién, qué plan pidió, qué monto se "
            "leyó, hasta cuándo quedaría cubierto) y los botones para aprobarlo o rechazarlo. Tú no apruebas ni "
            "rechazas: lo hace el administrador con los botones. Sin argumentos manda el pendiente más antiguo "
            "(«el siguiente»); con `nombre`, el pendiente de ese atleta; con `comprobante`, ese número. Manda uno "
            "por llamada: para revisar varios, espera a que decidan el anterior."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "comprobante": {"type": "integer", "description": "Número del comprobante, de `comprobantes`."},
                "nombre": {"type": "string", "description": "Nombre y/o apellido del atleta, o su código ATLETA_NN."},
                "grupo": {"type": "string", "description": "Nombre del grupo, para distinguir homónimos."},
            },
        },
    },
    {
        "name": "rechazar_comprobante",
        "description": (
            "Propone rechazar un comprobante pendiente con un motivo que no está entre los botones de la tarjeta. "
            "No lo rechaza: muestra al administrador el motivo, que es lo que recibirá el atleta por correo, con "
            "los botones Rechazar y Cancelar. Úsala solo si el administrador te dio el motivo; escríbelo en una o "
            "dos frases dirigidas al atleta, sin inventar datos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "comprobante": {"type": "integer", "description": "Número del comprobante."},
                "motivo": {"type": "string", "description": "Lo que leerá el atleta: por qué no se aceptó y qué hacer."},
            },
            "required": ["comprobante", "motivo"],
        },
    },
]

PREFERENCE_SCHEMA: dict[str, Any] = {
    "name": "guardar_preferencia",
    "description": (
        "Propone guardar una regla permanente cuando un administrador pide algo como «siempre que te pida esto, "
        "mándalo así» o «cuando diga runners, entiende atletas activos». Sirve para cómo presentas las cosas y para "
        "cómo interpretas lo que te piden; no para ver más datos de los que tus herramientas dan ni para saltarte una "
        "confirmación. NO la guarda: muestra la regla con los botones Guardar y Cancelar, y solo existe si el "
        "administrador pulsa Guardar; después de llamarla no digas que quedó guardada.\n"
        "Antes de proponer, compárala con las reglas ya guardadas (están numeradas al final de tus instrucciones). Si "
        "la nueva contradice a alguna o dice lo mismo, no la agregues tal cual: redacta UNA regla que resuelva el "
        "choque, pásala en `regla` y pon en `reemplaza` los números de las que sustituye. En tu respuesta dile al "
        "administrador con cuál chocaba, citándola, y por qué propones esa redacción. Si no es claro cuál de las dos "
        "quiere conservar, pregúntale antes de llamar a la herramienta.\n"
        "Escribe la regla en una frase clara, en imperativo, sin nombres de atletas ni datos de nadie."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "regla": {"type": "string", "description": "La regla, en una frase."},
            "reemplaza": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Números de las reglas guardadas que esta sustituye. Vacío si no choca con ninguna.",
            },
        },
        "required": ["regla"],
    },
}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _text(args: dict[str, Any], key: str, minimum: int = 2, maximum: int = 60) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not (minimum <= len(value.strip()) <= maximum):
        raise ValueError(f"`{key}` must be text of {minimum} to {maximum} characters")
    return value.strip()


def _iso_date(args: dict[str, Any], key: str) -> Optional[str]:
    value = args.get(key)
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise ValueError(f"`{key}` must be a date as YYYY-MM-DD") from None


STATUSES = {"todos": "all", "activos": "active", "inactivos": "inactive"}


def _member_status(args: dict[str, Any]) -> str:
    """`estado` as the API spells it. Everyone, active and paused, unless the admin narrowed it."""
    value = args.get("estado") or "todos"
    if value not in STATUSES:
        raise ValueError("`estado` must be todos, activos or inactivos")
    return STATUSES[value]


EVENT_STATUSES = {"inscritos": "registered", "ya_paso": "past", "con_tiempo": "with_time"}
MAX_FILTERS = 8
# A longer list than this goes to the chat as a CSV file instead of text.
QUERY_ROWS = 50
# The most the API returns in one query.
QUERY_LIMIT = 500
# Rows the model may read at once. With a period each person costs one more API call, so fewer.
ANALYZE_ROWS = 60
ANALYZE_ROWS_WITH_PERIOD = 25
ANALYZE_CONCURRENCY = 5
# Same zone as the API's "today".
TZ = ZoneInfo("America/Monterrey")
CHART_DEFAULT_WEEKS = 8
CHART_KINDS = ("semanal", "ranking", "dispersion")
# A choice button's callback data: `q:<who may answer>:<option number>`. The option's text is read back from
# the message's own keyboard when it is clicked, so nothing has to be stored.
CHOICE = "q"
MAX_CHOICES = 8
CHOICE_LABEL_MAX = 40


def choice_buttons(labels: list[str], telegram_user_id: int) -> list[tuple[str, str]]:
    return [(label, f"{CHOICE}:{telegram_user_id}:{i}") for i, label in enumerate(labels)]


def parse_choice(data: str) -> Optional[tuple[int, int]]:
    """`q:<user>:<n>` -> (user, n); None for anything else."""
    kind, _, rest = (data or "").partition(":")
    user, _, index = rest.partition(":")
    if kind != CHOICE or not user.isascii() or not user.isdigit() or not index.isascii() or not index.isdigit():
        return None
    return int(user), int(index)


def _integer(args: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not (minimum <= value <= maximum):
        raise ValueError(f"`{key}` must be a whole number from {minimum} to {maximum}")
    return value


def _window(raw: dict[str, Any]) -> dict[str, str]:
    start, end = _iso_date(raw, "desde"), _iso_date(raw, "hasta")
    if not start or not end:
        raise ValueError(f"a `{raw['tipo']}` filter needs `desde` and `hasta`")
    if start > end:
        raise ValueError("`desde` is after `hasta`")
    return {"from": start, "to": end}


def _filter(raw: Any) -> dict[str, Any]:
    """One filter as the API spells it. The API validates again; this only gives the model a clear error."""
    if not isinstance(raw, dict):
        raise ValueError("each filter must be an object with `tipo`")
    kind = raw.get("tipo")
    if kind == "evento":
        status = raw.get("condicion") or "inscritos"
        if status not in EVENT_STATUSES:
            raise ValueError("`condicion` must be inscritos, ya_paso or con_tiempo")
        out: dict[str, Any] = {"type": "event", "name": _text(raw, "nombre"), "status": EVENT_STATUSES[status]}
        if raw.get("anio") is not None:
            out["year"] = _integer(raw, "anio", 2000, 2100)
        return out
    if kind == "grupo":
        out = {"type": "group", "name": _text(raw, "nombre")}
        others = raw.get("otros") or []
        if not isinstance(others, list) or len(others) > 10:
            raise ValueError("`otros` must be a list of at most 10 group names")
        if others:
            out["also"] = [_text({"otros": other}, "otros") for other in others]
        return out
    if kind == "pago":
        return {"type": "paid", **_window(raw)}
    if kind == "entrenos":
        return {"type": "workouts", **_window(raw), "min_done": _integer(raw, "minimo", 1, 366)}
    if kind == "membresia":
        status = raw.get("situacion")
        if status not in MEMBERSHIP_STATUSES:
            raise ValueError("a `membresia` filter needs `situacion`: vigente, vencida, sin_membresia or vence")
        out = {"type": "membership", "status": MEMBERSHIP_STATUSES[status]}
        return {**out, **_window(raw)} if status == "vence" else out
    if kind == "comprobante":
        status = raw.get("situacion")
        if status not in RECEIPT_STATUSES:
            raise ValueError("a `comprobante` filter needs `situacion`: pendiente, aprobado or rechazado")
        out = {"type": "receipt", "status": RECEIPT_STATUSES[status]}
        if isinstance(raw.get("beneficio"), bool):
            out["benefit"] = raw["beneficio"]
        for key, name in (("desde", "from"), ("hasta", "to")):
            if _iso_date(raw, key):
                out[name] = _iso_date(raw, key)
        return out
    if kind == "perfil":
        out = {"type": "profile"}
        if raw.get("nivel") is not None:
            out["level"] = _integer(raw, "nivel", 0, 200)
        if raw.get("sede"):
            out["sede"] = _text(raw, "sede")
        if raw.get("genero"):
            if raw["genero"] not in GENDERS:
                raise ValueError("`genero` must be femenino or masculino")
            out["gender"] = GENDERS[raw["genero"]]
        if isinstance(raw.get("reloj"), bool):
            out["watch"] = raw["reloj"]
        if len(out) == 1:
            raise ValueError("a `perfil` filter needs `nivel`, `sede`, `genero` or `reloj`")
        return out
    if kind == "faltas":
        out = {"type": "missed", **_window(raw)}
        if raw.get("minimo") is not None:
            out["min_missed"] = _integer(raw, "minimo", 1, 366)
        return out
    raise ValueError("`tipo` must be evento, pago, grupo, entrenos, membresia, comprobante, perfil or faltas")


MEMBERSHIP_STATUSES = {"vigente": "current", "vencida": "expired", "sin_membresia": "none", "vence": "expires"}
RECEIPT_STATUSES = {"pendiente": "pending", "aprobado": "approved", "rechazado": "rejected"}
GENDERS = {"femenino": "female", "masculino": "male"}


METRICS = {"personas": "athletes", "pagos": "payments", "entrenos": "workouts", "km": "distance_km", "score": "score_avg"}


def _filters(args: dict[str, Any]) -> list[dict[str, Any]]:
    filters = args.get("filtros") or []
    if not isinstance(filters, list) or len(filters) > MAX_FILTERS:
        raise ValueError(f"`filtros` must be a list of at most {MAX_FILTERS} filters")
    return [_filter(f) for f in filters]


def _figures(found: dict[str, Any]) -> str:
    """The totals as one line for the model. Only numbers: nothing here identifies anyone."""
    parts = [f"Personas: {found['athletes']}."]
    period = found.get("period")
    if period:
        parts.append(f"Periodo: {period['from']} a {period['to']}.")
    payments = found.get("payments")
    if payments:
        entry = f"Pagos aprobados: {payments['count']}, suman ${payments['total']:,.2f} MXN"
        if payments.get("without_amount"):
            entry += f" ({payments['without_amount']} sin monto capturado, no suman)"
        parts.append(entry + ".")
    workouts = found.get("workouts")
    if workouts:
        entry = f"Entrenos: {workouts['done']} hechos de {workouts['prescribed']} prescritos"
        if workouts.get("completion_pct") is not None:
            entry += f" ({workouts['completion_pct']}%)"
        parts.append(entry + ".")
    if "distance_km" in found:
        parts.append(f"Distancia: {found['distance_km']} km.")
    if "score_avg" in found:
        score = found["score_avg"]
        parts.append("Score promedio: sin entrenos prescritos." if score is None else f"Score promedio: {score}%.")
    return " ".join(parts)


def _row(code: str, person: dict[str, Any], summary: Optional[dict[str, Any]]) -> str:
    """One person for the model: a code and figures, nothing that is free text."""
    parts = [code, person.get("role") or "sin rol", f"grupo {(person.get('group') or 'ninguno').strip()}"]
    parts.append("activo" if person.get("active", True) else "inactivo")
    for e in person.get("events", []):
        result = f" tiempo {_race_time(e['time_result'])}" if e.get("time_result") else ""
        if "goal" in e:
            # Said in words, so a missing figure reads as "nobody recorded it" and not as "I cannot see it".
            goal = _goal_seconds(e["goal"])
            result = f" objetivo {_race_time(goal)}" if goal else " sin objetivo capturado"
            result += f", resultado {_race_time(e['time_result'])}" if e.get("time_result") else ", sin resultado registrado"
            if goal and e.get("time_result"):
                result += f", diferencia {_gap(goal, e['time_result'])}"
        parts.append(f"evento {e['event']} ({e['date']}){result}")
    payment = person.get("last_payment")
    if payment:
        amount = f" ${payment['amount']:,.2f}" if payment.get("amount") is not None else " sin monto"
        parts.append(f"último pago {payment['date']}{amount}")
    parts += _extras(person)
    if summary is not None:
        workouts = summary["workouts"]
        parts.append(f"entrenos {workouts['done']}/{workouts['prescribed']}")
        if workouts.get("pending"):
            parts.append(f"{workouts['pending']} por hacer (no cuentan todavía)")
        for label, key, unit in (("score", "score_avg", "%"), ("km", "distance_km", ""), ("ritmo", "avg_pace", " min/km"), ("FC", "avg_heart_rate", " lpm")):
            if summary.get(key) is not None:
                parts.append(f"{label} {summary[key]}{unit}")
    return " | ".join(parts)


def _positive(args: dict[str, Any], key: str) -> Optional[float]:
    raw = args.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= 500:
        raise ValueError(f"`{key}` must be a number of km between 0 and 500")
    return float(raw)


def _measures(row: dict[str, Any]) -> list[str]:
    """Duration, pace, heart rate and score of a workout or a lap, each only when there is one."""
    parts = []
    if row.get("duration_sec"):
        parts.append(_race_time(round(row["duration_sec"])))
    if row.get("pace"):
        parts.append(f"{row['pace']} min/km")
    if row.get("heart_rate"):
        parts.append(f"FC {row['heart_rate']:.0f} lpm")
    if row.get("score") is not None:
        parts.append(f"score {row['score']}%")
    return parts


def _workout_row(workout: dict[str, Any]) -> str:
    parts = [f"#{workout['id']}", workout["date"], workout.get("type") or "sin tipo", f"{workout['distance_km']} km"]
    parts += _measures(workout)
    if workout.get("laps"):
        parts.append(f"{workout['laps']} vueltas")
    return " | ".join(parts)


def _lap_row(lap: dict[str, Any]) -> str:
    distance = f"{lap['distance_m']:.0f} m" if lap.get("distance_m") else "sin distancia"
    return " | ".join([f"vuelta {lap['lap']}", distance, *_measures(lap)])


WORKOUT_COLUMNS = [
    ("Fecha", 1.1, "left"), ("Tipo", 2.0, "left"), ("Km", 1.0, "right"), ("Tiempo", 1.3, "right"),
    ("Ritmo", 1.0, "right"), ("FC", 0.8, "right"), ("Score", 1.0, "right"),
]
WHICH_EVENT = "Ese nombre coincide con varios eventos. ¿Cuál quieres?"
ASKED_WHICH_EVENT = (
    "Ese nombre coincide con varios eventos: {events}. No mandé la consulta; ya le pregunté cuál con botones. No hagas "
    "nada más en este turno: su respuesta llega como su siguiente mensaje. Entonces repite la consulta con el nombre "
    "exacto y `anio`; si elige «{everything}», pon un filtro de evento por cada uno."
)
WHICH_GROUP = "Ese nombre coincide con varios grupos. ¿Cuál quieres?"
ASKED_WHICH_GROUP = (
    "Ese nombre coincide con varios grupos: {groups}. No mandé la consulta; ya le pregunté cuál con botones. No hagas "
    "nada más en este turno: su respuesta llega como su siguiente mensaje. Entonces repite la consulta con el nombre "
    "exacto; si elige «{everything}», pon uno en `nombre` y los demás en `otros`."
)
TOO_MANY_GROUPS = (
    "Ese nombre coincide con {count} grupos: {groups}. No mandé nada al chat. Pregúntale cuáles quiere y repite la "
    "consulta con los nombres exactos (uno en `nombre`, los demás en `otros`)."
)
# Too many to fit as buttons: the model narrows it down in words.
TOO_MANY_EVENTS = (
    "Ese nombre coincide con {count} eventos: {events}. No mandé nada al chat. Pregúntale de qué año o distancia y "
    "repite la consulta con el nombre exacto y `anio`."
)


# What the admin wrote in the conversation the running tool belongs to. Per task: one Toolbox serves every topic.
_SAID: ContextVar[Optional[set[str]]] = ContextVar("said", default=None)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _fold(text))


class AmbiguousEvent(Exception):
    """An event name that fits more races than the admin asked for."""

    def __init__(self, events: list[str]):
        super().__init__("; ".join(events))
        self.events = events


def _event_label(matched: str) -> str:
    """`42k Berlin 2026 (2026-09-27)` -> `42k Berlin 2026 · 27 sep 2026`, short enough for a button."""
    name, _, day = matched.rpartition(" (")
    when = _day_year(day.rstrip(")")) if name else ""
    name = name or matched
    return f"{_clip(name, CHOICE_LABEL_MAX - len(when) - 3)} · {when}" if when else _clip(name, CHOICE_LABEL_MAX)


class AmbiguousGroup(Exception):
    """A group name that fits more groups than the admin named."""

    def __init__(self, groups: list[str]):
        super().__init__("; ".join(groups))
        self.groups = groups


def which_group(groups: list[str], telegram_user_id: int) -> ToolResult:
    labels = [_clip(g.strip(), CHOICE_LABEL_MAX) for g in groups]
    if len(groups) >= MAX_CHOICES or len(set(labels)) != len(labels):
        return ToolResult(None, TOO_MANY_GROUPS.format(count=len(groups), groups="; ".join(groups)), is_error=True)
    everything = "Ambos" if len(groups) == 2 else "Todos"
    return ToolResult(
        WHICH_GROUP,
        ASKED_WHICH_GROUP.format(groups="; ".join(groups), everything=everything),
        buttons=choice_buttons([*labels, everything], telegram_user_id),
    )


def which_event(events: list[str], telegram_user_id: int) -> ToolResult:
    """The question the admin gets instead of a result built on a guess: one button per race, and one for all."""
    labels = [_event_label(e) for e in events]
    if len(events) >= MAX_CHOICES or len(set(labels)) != len(labels):
        return ToolResult(None, TOO_MANY_EVENTS.format(count=len(events), events="; ".join(events)), is_error=True)
    everything = "Ambos" if len(events) == 2 else "Todos"
    return ToolResult(
        WHICH_EVENT,
        ASKED_WHICH_EVENT.format(events="; ".join(events), everything=everything),
        buttons=choice_buttons([*labels, everything], telegram_user_id),
    )
ROSTER_COLUMNS = [
    ("Atleta", 3.0, "left"), ("Grupo", 2.2, "left"), ("Objetivo", 1.5, "right"),
    ("Resultado", 1.6, "right"), ("Diferencia", 1.2, "right"),
]
# What fits in those two columns before running into the next one.
ROSTER_NAME_MAX, ROSTER_GROUP_MAX = 22, 18
LAP_COLUMNS = [
    ("Vuelta", 0.7, "left"), ("Distancia", 1.4, "right"), ("Tiempo", 1.5, "right"),
    ("Ritmo", 1.3, "right"), ("FC", 1.2, "right"), ("Score", 1.2, "right"),
]
FORMATS = ("auto", "imagen", "csv")
SENT = "{what}: no repitas sus cifras; comenta en dos o tres líneas lo que importa."
WORKOUT_CSV = ["Fecha", "Tipo", "Km", "Tiempo", "Ritmo (min/km)", "FC (lpm)", "Score (%)", "Vueltas"]
LAP_CSV = ["Vuelta", "Distancia (m)", "Tiempo", "Ritmo (min/km)", "FC (lpm)", "Score (%)"]


RECEIPTS_LIMIT = 200
PLAN_STATUS = {"done": "Hecho", "missed": "No hecho", "upcoming": "Por hacer"}
PLAN_COLUMNS = [
    ("Fecha", 1.1, "left"), ("Tipo", 2.0, "left"), ("Estado", 1.3, "left"), ("Km", 0.9, "right"),
    ("Tiempo", 1.3, "right"), ("Ritmo", 1.0, "right"), ("Score", 1.0, "right"),
]
RECEIPT_COLUMNS = [
    ("Subido", 1.7, "left"), ("Atleta", 3.2, "left"), ("Estado", 1.4, "left"), ("Tipo", 1.3, "left"),
    ("Meses", 0.8, "right"), ("Monto", 1.6, "right"),
]


GARMIN_COLUMNS = [
    ("Fecha", 1.3, "left"), ("Atleta", 3.6, "left"), ("Tipo", 2.6, "left"), ("Estado", 2.2, "left"),
    ("Intentos", 1.1, "right"),
]
NAME_CELL = 26
MESSAGE_COLUMNS = [
    ("Fecha", 1.3, "left"), ("Canal", 1.0, "left"), ("Asunto", 3.4, "left"), ("Para", 1.0, "right"),
    ("Aceptados", 1.6, "right"), ("Fallidos", 1.5, "right"), ("Sin destino", 1.8, "right"),
]
CHANNEL_NAMES = {"email": "correo", "push": "push"}
CHANNELS = {"correo": "email", "push": "push", "ambos": "both"}
SUBJECT_MAX = 45
# The card is one Telegram message, rewritten with the outcome once decided: it has to fit in 4096.
ANNOUNCEMENT_MAX = 3000
NAMES_ON_CARD = 10


APPLICATION_STATUSES = {"pendiente": "pending", "espera": "waiting", "rechazada": "rejected"}
APPLICATION_COLUMNS = [
    ("Fecha", 1.6, "left"), ("Nombre", 3.6, "left"), ("Ciudad", 2.6, "left"), ("Cuestionario", 1.8, "left"),
]
APPLICATION_STATE = {
    "signed_up": "sin cuestionario", "awaiting_coach": "espera respuesta", "waiting_list": "en lista de espera",
    "rejected": "rechazada",
}


def _application_facts(a: dict[str, Any]) -> list[str]:
    """What the model may know about an application: no name and nothing the person typed."""
    facts = ["cuestionario contestado" if a["questionnaire"] else "sin cuestionario"]
    if a["status"] in ("waiting_list", "rejected"):
        facts.append(APPLICATION_STATE[a["status"]])
    if a.get("requested"):
        facts.append(f"solicitó el {a['requested']}")
    facts += [str(v) for v in (a.get("city"), a.get("gender")) if v]
    if a.get("age"):
        facts.append(f"unos {a['age']} años")
    return facts


def application_card(a: dict[str, Any]) -> str:
    lines = [f"Solicitud de {a['name']}"]
    where = " · ".join(str(v) for v in (a.get("city"), a.get("gender"), f"unos {a['age']} años" if a.get("age") else None) if v)
    if where:
        lines.append(where)
    if a.get("requested"):
        lines.append(f"La mandó el {_day_year(a['requested'])}")
    lines.append("Cuestionario: contestado" if a["questionnaire"] else "Cuestionario: sin contestar (no se puede aceptar todavía)")
    if a["status"] in ("waiting_list", "rejected"):
        lines.append(f"Hoy: {APPLICATION_STATE[a['status']]}")
    if a.get("comment"):
        lines.append(f"Comentario: «{a['comment']}»")
    return "\n".join(lines)


def _seconds(text: str) -> int:
    """`3:42:10` -> seconds. Hours, minutes and seconds, all three: `1:45` could mean two things."""
    parts = text.strip().split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise ValueError("`tiempo` must be H:MM:SS, for example 3:42:10")
    hours, minutes, seconds = (int(p) for p in parts)
    if minutes > 59 or seconds > 59 or not 0 < hours * 3600 + minutes * 60 + seconds <= 86400:
        raise ValueError("`tiempo` must be H:MM:SS, for example 3:42:10")
    return hours * 3600 + minutes * 60 + seconds


def _clock(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _delivery_cells(message: dict[str, Any]) -> list[str]:
    d = message["delivery"]
    failed = d["failed"] + d["rejected"]
    return [str(message["recipients"]), str(d["accepted"]), str(failed), str(d["no_destination"])]


def _audience_line(found: dict[str, Any], everyone: bool, named: int) -> str:
    """Who an announcement goes to, in the words of the card."""
    matched = found.get("matched") or {}
    parts = []
    if everyone:
        parts.append("todos los activos")
    if matched.get("groups"):
        parts.append("grupos " + ", ".join(matched["groups"]))
    if matched.get("events"):
        parts.append("eventos " + ", ".join(matched["events"]))
    if named:
        parts.append(f"{named} nombrado(s) uno por uno")
    return " · ".join(parts) or "los que cumplen los filtros"


def _plan_cells(workout: dict[str, Any], blank: str = charts.EMPTY_CELL) -> list[str]:
    """One day of a plan as table cells. What is still ahead shows the estimate, marked with `~`."""
    done = workout["status"] == "done"
    km = workout.get("distance_km") if done else workout.get("estimated_km")
    seconds = workout.get("duration_sec") if done else workout.get("estimated_sec")
    mark = "" if done else "~"
    return [
        _day(workout["date"]),
        workout.get("type") or blank,
        PLAN_STATUS[workout["status"]],
        f"{mark}{km:.1f}" if km else blank,
        mark + _race_time(round(seconds)) if seconds else blank,
        workout.get("pace") or blank,
        f"{_num(workout['score'])}%" if done and workout.get("score") is not None else blank,
    ]


def _plan_row(workout: dict[str, Any]) -> str:
    parts = [workout["date"], workout.get("type") or "sin tipo", PLAN_STATUS[workout["status"]].lower()]
    if workout["status"] == "done":
        parts.append(f"#{workout['id']}")
        parts.append(f"{workout['distance_km']} km")
        parts += _measures(workout)
    else:
        if workout.get("estimated_km"):
            parts.append(f"estimado {workout['estimated_km']} km")
        if workout.get("estimated_sec"):
            parts.append(f"estimado {_race_time(round(workout['estimated_sec']))}")
    return " | ".join(parts)


def _coverage(membership: dict[str, Any]) -> str:
    until = membership.get("covered_until")
    if not until:
        return "sin membresía registrada"
    verb = "cubierto hasta" if until >= date.today().isoformat() else "su membresía venció el"
    return f"{verb} {_day_year(until)}"


def _receipt_cells(receipt: dict[str, Any]) -> list[str]:
    amount = receipt.get("amount")
    return [
        _day_year(receipt["uploaded"]) if receipt.get("uploaded") else charts.EMPTY_CELL,
        RECEIPT_STATUS.get(receipt["status"], receipt["status"]).capitalize(),
        "Beneficio" if receipt.get("benefit") else "Pago",
        str(receipt["months"]) if receipt.get("months") else charts.EMPTY_CELL,
        _money(amount) if amount else charts.EMPTY_CELL,
    ]


def _receipt_row(receipt: dict[str, Any]) -> str:
    parts = [
        f"subido {receipt.get('uploaded') or 'sin fecha'}",
        RECEIPT_STATUS.get(receipt["status"], receipt["status"]),
        "beneficio" if receipt.get("benefit") else "pago",
    ]
    if receipt.get("months"):
        parts.append(f"{receipt['months']} mes(es)")
    if receipt.get("amount"):
        parts.append(f"${receipt['amount']:,.2f}")
    if receipt.get("paid"):
        parts.append(f"fecha de pago {receipt['paid']}")
    return " | ".join(parts)


RECEIPT_FILES = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif", "image/webp": "webp", "application/pdf": "pdf"}
READINGS = {"pendiente": "La lectura automática sigue en curso.", "fallo": "La lectura automática falló.", "omitida": ""}


def _reading(receipt: dict[str, Any]) -> str:
    """How what was read off the receipt compares with the plan asked for. Figures only."""
    if receipt.get("benefit"):
        return "Es un beneficio: no lleva monto y los meses los elige quien aprueba."
    amount, expected = receipt.get("amount"), receipt.get("expected")
    if not amount:
        return READINGS.get(receipt.get("reading") or "", "") or "No se leyó ningún monto."
    if expected and abs(amount - expected) >= 0.01:
        return f"Ojo: se leyó {_money(amount)} y el plan cuesta {_money(expected)}."
    return f"El monto leído, {_money(amount)}, coincide con el plan."


def _receipt_card(receipt_id: int, data: dict[str, Any]) -> str:
    """What the admin reads before deciding. The first paragraph is what stays once it is decided."""
    receipt, plans = data["receipt"], data["plans"]
    if receipt.get("benefit"):
        asked = "Beneficio, sin costo"
    else:
        asked = f"{receipt['months']} {'mes' if receipt['months'] == 1 else 'meses'}"
        asked += f" ({_money(receipt['expected'])})" if receipt.get("expected") else ""
    head = [f"Comprobante #{receipt_id} · {data['athlete']['name']}", f"Pidió: {asked}"]
    if receipt.get("uploaded"):
        head.append(f"Subido el {_day_year(receipt['uploaded'])}")
    body = [_reading(receipt)]
    read = []
    if receipt.get("paid"):
        read.append(f"fecha de pago {_day_year(receipt['paid'])}")
    if receipt.get("reference"):
        read.append(f"referencia {receipt['reference']}")
    if read:
        body.append("Leído del comprobante: " + ", ".join(read) + ".")
    if not receipt.get("file"):
        body.append("Este pago se registró sin archivo.")
    body.append(f"Hoy: {_coverage(data.get('membership') or {})}.")
    if receipt["status"] == "pending":
        options = (f"{p['months']} {'mes' if p['months'] == 1 else 'meses'} → {_day_year(p['covers_until'])}" for p in plans)
        body.append("Si se aprueba hoy: " + " · ".join(options))
    else:
        body.append(f"Estado: {RECEIPT_STATUS.get(receipt['status'], receipt['status'])}.")
    return "\n".join(head) + "\n\n" + "\n".join(part for part in body if part)


SIGNUP = {
    "accepted": "aceptado", "rejected": "rechazado", "waiting_list": "en lista de espera",
    "signed_up": "sin cuestionario", "awaiting_coach": "esperando a un coach",
}


def _profile_facts(data: dict[str, Any]) -> list[str]:
    """A member's profile as short lines. Nothing here is text the member typed freely."""
    athlete = data["athlete"]
    facts = [f"Rol: {athlete.get('role') or 'sin rol'} · grupo: {(athlete.get('group') or 'ninguno').strip()}"]
    who = []
    if data.get("level") is not None:
        who.append(f"nivel {data['level']}")
    if data.get("sede"):
        who.append(f"sede {data['sede']}")
    if data.get("gender"):
        who.append(str(data["gender"]).lower())
    if data.get("age"):
        who.append(f"unos {data['age']} años")
    if who:
        joined = ", ".join(who)
        facts.append(joined[0].upper() + joined[1:])
    watch = data.get("watch") or {}
    brand = f" ({watch['brand']})" if watch.get("brand") else ""
    facts.append(("Reloj vinculado" if watch.get("linked") else "Sin reloj vinculado") + brand)
    facts.append("Membresía: " + _coverage(data.get("membership") or {}))
    if data.get("signup") and data["signup"] != "accepted":
        facts.append(f"Solicitud: {SIGNUP.get(data['signup'], data['signup'])}")
    cycle = data.get("cycle")
    if cycle:
        target = f", objetivo {cycle['target_time']}" if cycle.get("target_time") else ""
        facts.append(f"Evento principal: {cycle['event']} ({cycle['event_date']}){target}")
    return facts


def _format(args: dict[str, Any]) -> str:
    chosen = args.get("formato") or "auto"
    if chosen not in FORMATS:
        raise ValueError(f"`formato` must be one of {', '.join(FORMATS)}")
    return chosen


def _pages(rows: list[Any]) -> list[list[Any]]:
    """The rows in as few tables as fit them, all about the same size: 60 rows are two of 30, not 40 and 20."""
    count = math.ceil(len(rows) / charts.TABLE_MAX_ROWS)
    size = math.ceil(len(rows) / count)
    return [rows[start : start + size] for start in range(0, len(rows), size)]


def _csv_cells(row: dict[str, Any]) -> list[Any]:
    return [
        _race_time(round(row["duration_sec"])) if row.get("duration_sec") else "",
        row.get("pace") or "",
        f"{row['heart_rate']:.0f}" if row.get("heart_rate") else "",
        "" if row.get("score") is None else _num(row["score"]),
    ]


def _cells(row: dict[str, Any]) -> list[str]:
    """Time, pace, heart rate and score as table cells, with a dash where there is none."""
    return [
        _race_time(round(row["duration_sec"])) if row.get("duration_sec") else charts.EMPTY_CELL,
        row.get("pace") or charts.EMPTY_CELL,
        f"{row['heart_rate']:.0f}" if row.get("heart_rate") else charts.EMPTY_CELL,
        f"{_num(row['score'])}%" if row.get("score") is not None else charts.EMPTY_CELL,
    ]


def _distance(metres: Optional[float]) -> str:
    if not metres:
        return charts.EMPTY_CELL
    return f"{metres / 1000:.2f} km" if metres >= 1000 else f"{metres:.0f} m"


def _candidate_label(athlete: dict[str, Any]) -> str:
    group = athlete.get("group")
    label = f"{athlete['name']} · {group}" if group else athlete["name"]
    return label if len(label) <= 60 else label[:59].rstrip() + "…"


def _week_figures(week: dict[str, Any]) -> str:
    score = "sin score" if week["score_avg"] is None else f"score {week['score_avg']}"
    return f"{week['week_start']}: {week['done']}/{week['prescribed']} entrenos, {week['distance_km']} km, {score}"


def _week_spread(week: dict[str, Any]) -> str:
    if not week.get("scored"):
        return f"{week['week_start']}: nadie"
    return f"{week['week_start']}: {week['scored']} personas, {week['score_min']} / {week['score_median']} / {week['score_max']}"


def _understood(found: dict[str, Any]) -> str:
    """What the API matched, for the model: event and group names, never people."""
    matched = found.get("matched") or {}
    parts = []
    if matched.get("events"):
        parts.append("Eventos: " + "; ".join(matched["events"]) + ".")
    if matched.get("groups"):
        parts.append("Grupos: " + "; ".join(matched["groups"]) + ".")
    if found.get("notes"):
        parts.append("Avisos del API: " + "; ".join(found["notes"]) + ".")
    return (" " + " ".join(parts)) if parts else ""


def preference_summary(rule: str, user_id: int, replaced: tuple[str, ...] = ()) -> str:
    text = f"Regla permanente que pide el admin {user_id}:\n«{rule}»"
    if replaced:
        text += "\n\nReemplaza a:\n" + "\n".join(f"«{old}»" for old in replaced)
    return text


class Toolbox:
    def __init__(
        self,
        api: MuunganoApi,
        confirmations: Optional[Confirmations] = None,
        preferences: Optional[Preferences] = None,
    ):
        self._api = api
        self._confirmations = confirmations
        self._preferences = preferences
        # Without somewhere to keep proposals and rules, the model is not offered the tool at all.
        self._schemas = SCHEMAS + [PREFERENCE_SCHEMA] if confirmations and preferences else SCHEMAS
        if confirmations:
            self._schemas = self._schemas + RECEIPT_SCHEMAS + MEMBER_WRITE_SCHEMAS + [APPLICATION_SCHEMA, ANNOUNCEMENT_SCHEMA]

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    async def run(
        self,
        name: str,
        args: dict[str, Any],
        telegram_user_id: int,
        names: Optional[Pseudonyms] = None,
        said: Optional[str] = None,
    ) -> ToolResult:
        names = names if names is not None else Pseudonyms()
        _SAID.set(set(_words(said)) if said is not None else None)
        handlers = {
            "buscar_atleta": lambda a, u: self._search(a, u, names),
            "resumen_atleta": self._summary,
            "buscar_atletas": self._query,
            "cifras": self._aggregate,
            "catalogo": self._catalog,
            "preguntar": self._ask,
            "grafica": lambda a, u: self._chart(a, u, names),
            "consultar": lambda a, u: self._analyze(a, u, names),
            "entrenos_atleta": lambda a, u: self._workouts(a, u, names),
            "vueltas_entreno": lambda a, u: self._laps(a, u, names),
            "plan_atleta": lambda a, u: self._plan(a, u, names),
            "pagos_atleta": lambda a, u: self._payments(a, u, names),
            "perfil_atleta": lambda a, u: self._profile(a, u, names),
            "comprobantes": lambda a, u: self._receipts(a, u, names),
            "revisar_comprobante": lambda a, u: self._review(a, u, names),
            "rechazar_comprobante": lambda a, u: self._propose_rejection(a, u, names),
            "renovar_membresia": lambda a, u: self._propose_renewal(a, u, names),
            "pausar_atleta": lambda a, u: self._propose_access(a, u, names),
            "tiempo_carrera": lambda a, u: self._propose_race_time(a, u, names),
            "solicitudes": lambda a, u: self._applications(a, u, names),
            "revisar_solicitud": lambda a, u: self._review_application(a, u, names),
            "errores_garmin": lambda a, u: self._garmin_errors(a, u, names),
            "avisos_enviados": self._sent_messages,
            "proponer_aviso": lambda a, u: self._propose_announcement(a, u, names),
        }
        handlers["resumen_atleta"] = lambda a, u: self._summary(a, u, names)
        if self._confirmations and self._preferences:
            handlers["guardar_preferencia"] = self._propose_preference
        handler = handlers.get(name)
        if handler is None:
            return ToolResult(None, f"Unknown tool {name!r}", is_error=True)
        try:
            return await handler(args, telegram_user_id)
        except AmbiguousEvent as exc:
            return which_event(exc.events, telegram_user_id)
        except AmbiguousGroup as exc:
            return which_group(exc.groups, telegram_user_id)
        except PreferenceError as exc:
            return ToolResult(None, f"No se puede guardar: {exc}", is_error=True)
        except ValueError as exc:
            return ToolResult(None, str(exc), is_error=True)
        except ApiError as exc:
            # The admin only gets the model's paraphrase; the real cause has to be visible somewhere.
            log.warning("tool %s failed: API status %s: %s", name, exc.status, exc.message)
            return ToolResult(None, exc.message, is_error=True)

    async def _find(self, text: str, member_status: str, telegram_user_id: int) -> dict[str, Any]:
        return await self._api.get(
            "/assistant/athletes",
            telegram_user_id=telegram_user_id,
            params={"q": text, "member_status": member_status},
        )

    async def _filtered(self, path: str, telegram_user_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """A query by filters. Stopped when a name fits more races or groups than were asked for: nothing leaves on a guess."""
        found = await self._api.post(path, telegram_user_id=telegram_user_id, json=body)
        asked = sum(1 for f in body.get("filters") or [] if f.get("type") == "event")
        events = (found.get("matched") or {}).get("events") or []
        if asked and len(events) > asked:
            raise AmbiguousEvent(events)
        named = sum(1 + len(f.get("also") or []) for f in body.get("filters") or [] if f.get("type") == "group")
        groups = list(dict.fromkeys((found.get("matched") or {}).get("groups") or []))
        if named and len(groups) > named:
            raise AmbiguousGroup(groups)
        for f in body.get("filters") or []:
            if f.get("type") == "event":
                await self._not_guessed(f, telegram_user_id)
        return found

    async def _not_guessed(self, event: dict[str, Any], telegram_user_id: int) -> None:
        """Stop an event filter the model narrowed by itself: a year or a distance the admin never wrote.

        "Los de Berlin" asked as `42k Berlin 2026` fits one race, so nothing above objects. Asked again with only
        the admin's own words, it fits three: that is the question to put to them.
        """
        said = _SAID.get()
        if said is None:
            return
        words = _words(event["name"])
        own = [w for w in words if w in said]
        year = event.get("year")
        year_said = year is None or str(year) in said
        if (own == words and year_said) or len(" ".join(own)) < 2:
            return
        probe = {k: v for k, v in event.items() if k != "year" or year_said}
        probe["name"] = " ".join(own)
        body = {"filters": [probe], "member_status": "all", "count_only": True}
        found = await self._api.post("/assistant/athletes/query", telegram_user_id=telegram_user_id, json=body)
        events = (found.get("matched") or {}).get("events") or []
        if len(events) > 1:
            raise AmbiguousEvent(events)

    async def _search(self, args: dict[str, Any], telegram_user_id: int, names: Pseudonyms) -> ToolResult:
        if args.get("filtros"):
            return await self._search_within(args, telegram_user_id, names)
        found = await self._find(_text(args, "texto"), _member_status(args), telegram_user_id)
        athletes, total = found["athletes"], found["total"]
        return ToolResult(
            render_candidates(athletes, total, question=False),
            f"{total} resultado(s); ya los mostré en el chat.",
        )

    async def _search_within(self, args: dict[str, Any], telegram_user_id: int, names: Pseudonyms) -> ToolResult:
        """A name among those a filter matches: "Ari, la de Berlin". A few hits go to the model as codes, not to the chat."""
        wanted = _fold(_text(args, "texto"))
        body = {"filters": _filters(args), "member_status": _member_status(args), "limit": QUERY_LIMIT}
        found = await self._filtered("/assistant/athletes/query", telegram_user_id, body)
        hits = [a for a in found["athletes"] if wanted in _fold(a["name"])]
        if not hits:
            return ToolResult(None, f"Nadie con ese nombre entre las {found['total']} persona(s) de esos filtros.{_understood(found)}")
        if len(hits) > MAX_CHOICES:
            return ToolResult(
                render_candidates(hits, len(hits), question=False),
                f"{len(hits)} resultado(s); ya los mostré en el chat.{_understood(found)}",
            )
        codes = "; ".join(f"{names.code(a['id'], a['name'])} (grupo {(a.get('group') or 'ninguno').strip()})" for a in hits)
        return ToolResult(
            None,
            f"{len(hits)} coincidencia(s): {codes}. Usa el código como `nombre` en las demás herramientas.{_understood(found)}",
        )

    async def _query(self, args: dict[str, Any], telegram_user_id: int) -> ToolResult:
        body: dict[str, Any] = {"filters": _filters(args), "member_status": _member_status(args)}
        count_only = args.get("solo_contar") is True
        if count_only:
            body["count_only"] = True
        else:
            body["limit"] = QUERY_LIMIT
        found = await self._filtered("/assistant/athletes/query", telegram_user_id, body)

        total = found["total"]
        if count_only:
            return ToolResult(None, f"{total} persona(s) cumplen los filtros.{_understood(found)}")
        # Asked by event alone, the list is that race's entrants. With more filters it is about something else too.
        roster = self._roster(found) if all(f.get("type") == "event" for f in body["filters"]) else None
        if roster:
            return await roster
        if total <= QUERY_ROWS:
            return ToolResult(render_matches(found), f"{total} resultado(s); ya los mostré en el chat.{_understood(found)}")

        shown = found["returned"]
        caption = f"{total} personas. Va la lista completa en el archivo."
        left = ""
        if shown < total:
            caption = f"{total} personas. El archivo trae las primeras {shown}; acota los filtros para ver al resto."
            left = f" El archivo solo trae {shown}; dile que acote."
        return ToolResult(
            caption,
            f"{total} resultado(s); mandé la lista al chat como archivo CSV.{left}{_understood(found)}",
            files=[OutFile("atletas.csv", matches_csv(found))],
        )

    def _roster(self, found: dict[str, Any]) -> Optional[Awaitable[ToolResult]]:
        """The entrants of one race as a single titled picture. None when the list is about something else."""
        athletes = found["athletes"]
        entries = [a.get("events") or [] for a in athletes]
        if not athletes or found["total"] != len(athletes) or len(athletes) > charts.TABLE_MAX_ROWS:
            return None
        if any(len(e) != 1 for e in entries) or len({(e[0]["event"], e[0]["date"]) for e in entries}) != 1:
            return None
        event = entries[0][0]
        rows, with_goal, with_result = [], 0, 0
        beaten: dict[tuple[int, int], str] = {}
        for athlete, (entry,) in zip(athletes, entries):
            goal, result = _goal_seconds(entry.get("goal")), entry.get("time_result")
            with_goal += bool(goal)
            with_result += bool(result)
            rows.append([
                _clip(athlete["name"], ROSTER_NAME_MAX),
                _clip((athlete.get("group") or "").strip(), ROSTER_GROUP_MAX) or charts.EMPTY_CELL,
                _race_time(goal) if goal else "sin capturar",
                _race_time(result) if result else "sin resultado",
                _gap(goal, result) if goal and result else charts.EMPTY_CELL,
            ])
            if goal and result and int(result) <= goal:
                beaten[(len(rows) - 1, len(ROSTER_COLUMNS) - 1)] = charts.TABLE_GOOD
        total = len(athletes)
        to_model = (
            f"{total} inscrito(s) a {event['event']} ({event['date']}): {with_goal} con objetivo capturado, "
            f"{with_result} con resultado.{_understood(found)}"
        )
        return self._tables(
            "registro",
            "Evento",
            f"Registro a {event['event']}",
            f"{_day_year(event['date'])}  ·  {total} inscritos",
            ROSTER_COLUMNS,
            rows,
            to_model,
            colors=beaten,
        )

    async def _propose_preference(self, args: dict[str, Any], telegram_user_id: int) -> ToolResult:
        if not isinstance(args.get("regla"), str):
            raise ValueError("`regla` must be text")
        rule = clean(args["regla"])
        lines, rules = self._preferences.lines(), self._preferences.rules()
        numbers = args.get("reemplaza") or []
        if not isinstance(numbers, list) or any(
            isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= len(lines) for n in numbers
        ):
            raise ValueError(f"`reemplaza` must be a list of rule numbers from 1 to {len(lines)}")
        picked = sorted(set(numbers))
        self._preferences.check_room(replacing=len(picked))
        summary = preference_summary(rule, telegram_user_id, tuple(rules[n - 1] for n in picked))
        payload = {"rule": rule, "replace": [lines[n - 1] for n in picked]}
        action_id = await self._confirmations.propose(telegram_user_id, "preference_add", payload, summary)
        return ToolResult(
            summary,
            "Mostré la regla con los botones Guardar y Cancelar. Todavía NO está guardada: lo decide el administrador "
            "al pulsar. No digas que quedó guardada."
            + (" Explícale con cuál regla chocaba y por qué propones esta redacción." if picked else ""),
            buttons=confirmations.buttons(action_id, "Guardar"),
        )

    async def _ask(self, args: dict[str, Any], telegram_user_id: int) -> ToolResult:
        question = _text(args, "pregunta", minimum=3, maximum=300)
        options = args.get("opciones")
        if not isinstance(options, list) or not 2 <= len(options) <= MAX_CHOICES:
            raise ValueError(f"`opciones` must be a list of 2 to {MAX_CHOICES} texts")
        labels = []
        for option in options:
            if not isinstance(option, str) or not 1 <= len(option.strip()) <= CHOICE_LABEL_MAX:
                raise ValueError(f"each option must be text of 1 to {CHOICE_LABEL_MAX} characters")
            labels.append(option.strip())
        if len(set(labels)) != len(labels):
            raise ValueError("two options say the same")
        return ToolResult(
            question,
            "Pregunta enviada con botones. No hagas nada más en este turno: la respuesta llega como su siguiente mensaje.",
            buttons=choice_buttons(labels, telegram_user_id),
        )

    async def _catalog(self, args: dict[str, Any], telegram_user_id: int) -> ToolResult:
        found = await self._api.get("/assistant/catalog", telegram_user_id=telegram_user_id)
        groups = "; ".join(f"{g['name']} ({g['members']})" for g in found["groups"]) or "ninguno"
        events = "; ".join(f"{e['name']} ({e['date']})" for e in found["events"]) or "ninguno"
        return ToolResult(None, f"Grupos (miembros): {groups}.\nEventos (fecha): {events}.")

    async def _aggregate(self, args: dict[str, Any], telegram_user_id: int) -> ToolResult:
        wanted = args.get("metricas")
        if not isinstance(wanted, list) or not wanted or any(m not in METRICS for m in wanted):
            raise ValueError("`metricas` must be a list with any of: " + ", ".join(METRICS))
        metrics = list(dict.fromkeys(METRICS[m] for m in wanted))
        body: dict[str, Any] = {"filters": _filters(args), "member_status": _member_status(args), "metrics": metrics}
        start, end = _iso_date(args, "desde"), _iso_date(args, "hasta")
        if metrics != ["athletes"]:
            if not start or not end:
                raise ValueError("those metrics need `desde` and `hasta`")
            if start > end:
                raise ValueError("`desde` is after `hasta`")
            body["period"] = {"from": start, "to": end}
        found = await self._filtered("/assistant/athletes/aggregate", telegram_user_id, body)
        return ToolResult(None, _figures(found) + _understood(found))

    async def _analyze(self, args: dict[str, Any], telegram_user_id: int, names: Pseudonyms) -> ToolResult:
        start, end = _iso_date(args, "desde"), _iso_date(args, "hasta")
        if bool(start) != bool(end):
            raise ValueError("give both `desde` and `hasta`, or neither")
        if start and start > end:
            raise ValueError("`desde` is after `hasta`")
        cap = ANALYZE_ROWS_WITH_PERIOD if start else ANALYZE_ROWS
        body = {"filters": _filters(args), "member_status": _member_status(args), "limit": cap}
        found = await self._filtered("/assistant/athletes/query", telegram_user_id, body)
        total = found["total"]
        if total > cap:
            # Part of the list would read as the whole list: the analysis would be wrong without saying so.
            raise ValueError(f"{total} people match and at most {cap} can be analysed at once; narrow the filters")
        people = found["athletes"]
        if not people:
            return ToolResult(None, f"Nadie cumple esos filtros.{_understood(found)}")

        summaries: list[Optional[dict[str, Any]]] = [None] * len(people)
        if start:
            gate = asyncio.Semaphore(ANALYZE_CONCURRENCY)

            async def one(person: dict[str, Any]) -> dict[str, Any]:
                async with gate:
                    return await self._api.get(
                        f"/assistant/athletes/{person['id']}/summary",
                        telegram_user_id=telegram_user_id,
                        params={"from": start, "to": end},
                    )

            summaries = list(await asyncio.gather(*(one(p) for p in people)))

        rows = [_row(names.code(p["id"], p["name"]), p, s) for p, s in zip(people, summaries)]
        period = f" Periodo de entrenos: {start} a {end}." if start else ""
        return ToolResult(None, f"{total} persona(s).{period}{_understood(found)}\n" + "\n".join(rows))

    async def _pick(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> int | ToolResult:
        """The one athlete `nombre` (and `grupo`) points at, or what to tell the admin when it is none or several."""
        name = _text(args, "nombre")
        group = args.get("grupo")

        known = names.athlete_id(name) if names else None
        if known is not None:
            return known
        candidates = (await self._find(name, "all", telegram_user_id))["athletes"]
        if isinstance(group, str) and group.strip():
            wanted = _fold(group.strip())
            candidates = [a for a in candidates if wanted in _fold(a.get("group") or "")]

        if not candidates:
            return ToolResult("No encontré a nadie con ese nombre.", "Sin resultados; ya se lo dije al administrador.")
        if len(candidates) > 1:
            labels = [_candidate_label(a) for a in candidates]
            # Buttons only when each one says something different: two people with the same name and group need typing.
            unique = len(candidates) <= MAX_CHOICES and len(set(labels)) == len(labels)
            return ToolResult(
                render_candidates(candidates, len(candidates), question=True),
                f"Hay {len(candidates)} candidatos; ya le pregunté al administrador cuál. Espera su respuesta.",
                buttons=choice_buttons(labels, telegram_user_id) if unique else None,
            )
        return candidates[0]["id"]

    async def _chart(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms] = None) -> ToolResult:
        metric = args.get("metrica")
        if metric not in charts.METRICS:
            raise ValueError("`metrica` must be one of: " + ", ".join(charts.METRICS))
        kind = args.get("tipo") or "semanal"
        if kind not in CHART_KINDS:
            raise ValueError("`tipo` must be one of: " + ", ".join(CHART_KINDS))
        one = args.get("nombre") not in (None, "")
        if one and args.get("filtros"):
            raise ValueError("send `nombre` or `filtros`, not both")
        if one and kind != "semanal":
            raise ValueError(f"`{kind}` compares the people of a set: send `filtros`, not `nombre`")
        if kind == "dispersion" and metric != "score":
            raise ValueError("`dispersion` is only drawn for `metrica` score")

        start, end = _iso_date(args, "desde"), _iso_date(args, "hasta")
        today = datetime.now(TZ).date()
        end = end or today.isoformat()
        start = start or (date.fromisoformat(end) - timedelta(weeks=CHART_DEFAULT_WEEKS) + timedelta(days=1)).isoformat()
        if start > end:
            raise ValueError("`desde` is after `hasta`")
        body: dict[str, Any] = {"period": {"from": start, "to": end}}

        if one:
            athlete_id = await self._pick(args, telegram_user_id, names)
            if isinstance(athlete_id, ToolResult):
                return athlete_id
            body["athlete_id"] = athlete_id
        else:
            body.update(filters=_filters(args), member_status=_member_status(args))
        if kind == "ranking":
            body["per_athlete"] = True

        found = await self._filtered("/assistant/athletes/series", telegram_user_id, body)
        weeks = found["weeks"]
        if one:
            who = found["athlete"]["name"]
        else:
            people = found["athletes"]
            groups = (found.get("matched") or {}).get("groups") or []
            who = (" / ".join(groups) + " · " if groups else "") + ("1 persona" if people == 1 else f"{people} personas")
        subtitle = f"{who} · {_period(start, end)}"

        if kind == "ranking":
            people = found.get("people") or []
            ranked = len(charts.ranked(people, metric))
            if not ranked:
                return ToolResult(
                    f"No hay nada que graficar de {who} en ese periodo.",
                    "Nadie tuvo entrenos prescritos en ese periodo; ya se lo dije al administrador." + _understood(found),
                )
            png = await asyncio.to_thread(charts.ranking_png, people, metric, subtitle)
            shown = "todas" if ranked <= 2 * charts.RANKING_ENDS else f"las {charts.RANKING_ENDS} primeras y las {charts.RANKING_ENDS} últimas"
            # Names and each person's figure are in the picture only.
            acknowledgement = (
                f"Ranking de {metric} enviado al chat, del {start} al {end}: {ranked} personas con entrenos prescritos, "
                f"se muestran {shown}." + _understood(found)
            )
            return ToolResult("", acknowledgement, files=[OutFile(f"ranking_{metric}.png", png, photo=True)])

        if kind == "dispersion":
            if not charts.has_spread(weeks):
                return ToolResult(
                    f"No hay nada que graficar de {who} en ese periodo.",
                    "Nadie tuvo entrenos prescritos en ese periodo; ya se lo dije al administrador." + _understood(found),
                )
            png = await asyncio.to_thread(charts.spread_png, weeks, subtitle)
            acknowledgement = (
                f"Gráfica de dispersión del score enviada al chat, del {start} al {end}. Por semana (personas con "
                "entrenos: mínimo / mediana / máximo): " + "; ".join(_week_spread(w) for w in weeks) + "." + _understood(found)
            )
            return ToolResult("", acknowledgement, files=[OutFile("dispersion_score.png", png, photo=True)])

        if not charts.has_data(weeks, metric):
            return ToolResult(
                f"No hay nada que graficar de {who} en ese periodo.",
                "Sin datos en ese periodo; ya se lo dije al administrador." + ("" if one else _understood(found)),
            )

        # Drawing is CPU work: off the event loop, so the bot keeps answering while it renders.
        png = await asyncio.to_thread(charts.weekly_png, weeks, metric, subtitle)
        acknowledgement = f"Gráfica de {metric} enviada al chat: {len(weeks)} semanas, del {start} al {end}."
        if not one:
            # Totals of a set identify nobody, like `cifras`. One athlete's weeks stay out of the model.
            acknowledgement += " Por semana: " + "; ".join(_week_figures(w) for w in weeks) + "." + _understood(found)
        return ToolResult("", acknowledgement, files=[OutFile(f"{metric}.png", png, photo=True)])

    async def _workouts(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        start, end = _iso_date(args, "desde"), _iso_date(args, "hasta")
        shortest, longest = _positive(args, "km_min"), _positive(args, "km_max")
        if shortest is not None and longest is not None and shortest > longest:
            raise ValueError("`km_min` is above `km_max`")
        layout = _format(args)
        athlete_id = await self._pick(args, telegram_user_id, names)
        if isinstance(athlete_id, ToolResult):
            return athlete_id

        params: dict[str, Any] = {}
        for key, value in (("from", start), ("to", end), ("min_km", shortest), ("max_km", longest)):
            if value is not None:
                params[key] = value
        if args.get("ciclo"):
            params["use_cycle"] = "true"
        data = await self._api.get(
            f"/assistant/athletes/{athlete_id}/workouts", telegram_user_id=telegram_user_id, params=params
        )
        period = f"del {data['period']['from']} al {data['period']['to']}"
        workouts = data["workouts"]
        if not workouts:
            return ToolResult(None, f"Sin entrenos hechos {period} con esos filtros.")
        kept = f" Van solo los {len(workouts)} más largos." if data.get("truncated") else ""
        to_model = (
            f"{data['total']} entreno(s) hechos {period}.{kept} El número tras # es el que pide `vueltas_entreno`.\n"
            + "\n".join(_workout_row(w) for w in workouts)
        )
        name = (data.get("athlete") or {}).get("name")
        span = _period(data["period"]["from"], data["period"]["to"])
        if layout == "csv" or (layout == "auto" and len(workouts) > charts.TABLE_MAX_ROWS):
            rows = [
                [w["date"], w.get("type") or "", w["distance_km"], *_csv_cells(w), w.get("laps") or ""] for w in workouts
            ]
            caption = f"{name + ': ' if name else ''}{len(workouts)} entrenos, {span}."
            return ToolResult(
                caption,
                to_model + "\n" + SENT.format(what="La lista ya salió al chat como archivo CSV"),
                files=[OutFile("entrenos.csv", table_csv(WORKOUT_CSV, rows))],
            )
        return await self._tables(
            "entrenos",
            "Entrenos",
            name or "Entrenos",
            f"{span}  ·  {len(workouts)} entrenos",
            WORKOUT_COLUMNS,
            [[_day(w["date"]), w.get("type") or charts.EMPTY_CELL, f"{w['distance_km']:.1f}", *_cells(w)] for w in workouts],
            to_model,
        )

    async def _deliver(
        self,
        layout: str,
        filename: str,
        label: str,
        title: str,
        subtitle: str,
        columns: list[tuple[str, float, str]],
        rows: list[list[str]],
        csv_header: list[str],
        csv_rows: list[list[Any]],
        to_model: str,
    ) -> ToolResult:
        """The rows to the chat: a table picture while one holds them, a CSV past that, or what the admin asked for."""
        if layout == "csv" or (layout == "auto" and len(rows) > charts.TABLE_MAX_ROWS):
            return ToolResult(
                f"{title}: {len(rows)} filas.",
                to_model + "\n" + SENT.format(what="La lista ya salió al chat como archivo CSV"),
                files=[OutFile(f"{filename}.csv", table_csv(csv_header, csv_rows))],
            )
        return await self._tables(filename, label, title, subtitle, columns, rows, to_model)

    async def _plan(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        start, end = _iso_date(args, "desde"), _iso_date(args, "hasta")
        if start and end and start > end:
            raise ValueError("`desde` is after `hasta`")
        layout = _format(args)
        athlete_id = await self._pick(args, telegram_user_id, names)
        if isinstance(athlete_id, ToolResult):
            return athlete_id
        params = {key: value for key, value in (("from", start), ("to", end)) if value}
        data = await self._api.get(f"/assistant/athletes/{athlete_id}/plan", telegram_user_id=telegram_user_id, params=params)
        span, workouts, counts = data["period"], data["workouts"], data["counts"]
        if not workouts:
            return ToolResult(None, f"Sin entrenos prescritos del {span['from']} al {span['to']}.")
        tally = f"{counts['done']} hechos, {counts['missed']} sin hacer, {counts['upcoming']} por hacer"
        to_model = f"Plan del {span['from']} al {span['to']} (hoy es {data['today']}): {tally}.\n" + "\n".join(
            _plan_row(w) for w in workouts
        )
        return await self._deliver(
            layout,
            "plan",
            "Plan",
            data["athlete"]["name"],
            f"{_period(span['from'], span['to'])}  ·  {tally}",
            PLAN_COLUMNS,
            [_plan_cells(w) for w in workouts],
            ["Fecha", "Tipo", "Estado", "Km", "Tiempo", "Ritmo (min/km)", "Score (%)"],
            [[w["date"], *_plan_cells(w, blank="")[1:]] for w in workouts],
            to_model,
        )

    async def _payments(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        athlete_id = await self._pick(args, telegram_user_id, names)
        if isinstance(athlete_id, ToolResult):
            return athlete_id
        data = await self._api.get(f"/assistant/athletes/{athlete_id}/payments", telegram_user_id=telegram_user_id)
        coverage = _coverage(data["membership"])
        receipts = data["receipts"]
        if not receipts:
            return ToolResult(f"{data['athlete']['name']}: {coverage}. Sin comprobantes.", f"{coverage}. Sin comprobantes.")
        to_model = f"{coverage}. {data['total']} comprobante(s):\n" + "\n".join(_receipt_row(r) for r in receipts)
        return await self._tables(
            "pagos",
            "Pagos",
            data["athlete"]["name"],
            coverage[0].upper() + coverage[1:],
            RECEIPT_COLUMNS[:1] + RECEIPT_COLUMNS[2:],
            [_receipt_cells(r) for r in receipts],
            to_model,
        )

    async def _receipts(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        status = args.get("situacion") or "pendiente"
        if status not in RECEIPT_STATUSES:
            raise ValueError("`situacion` must be pendiente, aprobado or rechazado")
        start, end = _iso_date(args, "desde"), _iso_date(args, "hasta")
        if start and end and start > end:
            raise ValueError("`desde` is after `hasta`")
        layout = _format(args)
        params: dict[str, Any] = {"status": RECEIPT_STATUSES[status], "limit": RECEIPTS_LIMIT}
        params.update({key: value for key, value in (("from", start), ("to", end)) if value})
        if isinstance(args.get("beneficio"), bool):
            params["benefit"] = "true" if args["beneficio"] else "false"
        data = await self._api.get("/assistant/receipts", telegram_user_id=telegram_user_id, params=params)
        receipts, total = data["receipts"], data["total"]
        plural = f"comprobante(s) {status}(s)"
        if not receipts:
            return ToolResult(None, f"Ningún comprobante {status} con esos filtros.")
        names = names if names is not None else Pseudonyms()
        left = f" Van solo los {len(receipts)} más recientes." if data.get("truncated") else ""
        to_model = f"{total} {plural}.{left}\n" + "\n".join(
            f"{names.code(r['athlete']['id'], r['athlete']['name'])} | {_receipt_row(r)}" for r in receipts
        )
        return await self._deliver(
            layout,
            "comprobantes",
            "Comprobantes",
            f"{total} {status}s" if total != 1 else f"1 {status}",
            "Del más reciente al más antiguo" + (f"  ·  los {len(receipts)} más recientes" if data.get("truncated") else ""),
            RECEIPT_COLUMNS,
            [[_receipt_cells(r)[0], r["athlete"]["name"], *_receipt_cells(r)[1:]] for r in receipts],
            ["Subido", "Atleta", "Estado", "Tipo", "Meses", "Monto", "Fecha de pago", "Motivo de rechazo"],
            [
                [
                    r.get("uploaded") or "", r["athlete"]["name"], RECEIPT_STATUS.get(r["status"], r["status"]),
                    "beneficio" if r.get("benefit") else "pago", r.get("months") or "",
                    "" if r.get("amount") is None else r["amount"], r.get("paid") or "", r.get("reason") or "",
                ]
                for r in receipts
            ],
            to_model,
        )

    async def _review(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        names = names if names is not None else Pseudonyms()
        if args.get("comprobante") is not None:
            receipt_id = _integer(args, "comprobante", 1, 2**31 - 1)
        elif args.get("nombre"):
            athlete_id = await self._pick(args, telegram_user_id, names)
            if isinstance(athlete_id, ToolResult):
                return athlete_id
            mine = await self._api.get(f"/assistant/athletes/{athlete_id}/payments", telegram_user_id=telegram_user_id)
            waiting = [r["id"] for r in mine["receipts"] if r["status"] == "pending"]
            if not waiting:
                return ToolResult(None, "Ese atleta no tiene comprobantes pendientes.")
            receipt_id = waiting[-1]
        else:
            queue = await self._api.get(
                "/assistant/receipts", telegram_user_id=telegram_user_id, params={"status": "pending", "limit": RECEIPTS_LIMIT}
            )
            if not queue["receipts"]:
                return ToolResult(None, "No hay comprobantes pendientes.")
            receipt_id = queue["receipts"][-1]["id"]

        data = await self._api.get(f"/assistant/receipts/{receipt_id}", telegram_user_id=telegram_user_id)
        receipt, athlete = data["receipt"], data["athlete"]
        files, missing = [], ""
        if receipt.get("file"):
            try:
                content, mime = await self._api.get_file(f"/assistant/receipts/{receipt_id}/file", telegram_user_id=telegram_user_id)
            except ApiError as exc:
                # The card is still worth sending: the admin can open the file in the console.
                log.warning("receipt %s: its file could not be fetched: API status %s", receipt_id, exc.status)
                missing = "\nNo pude traer el archivo; ábrelo en la consola."
            else:
                kind = RECEIPT_FILES.get(mime)
                files = [OutFile(f"comprobante_{receipt_id}.{kind or 'bin'}", content, photo=kind in ("jpg", "png"), mime=mime)]

        card = _receipt_card(receipt_id, data) + missing
        code = names.code(athlete["id"], athlete["name"])
        seen = f"Comprobante #{receipt_id} de {code}: {_receipt_row(receipt)}. {_reading(receipt)}"
        if receipt["status"] != "pending" or self._confirmations is None:
            return ToolResult(card, f"{seen} Ya no está pendiente: salió al chat sin botones.", files=files)
        action_id = await self._confirmations.propose(
            telegram_user_id, "receipt", {"receipt": receipt_id}, card.split("\n\n")[0]
        )
        months = [plan["months"] for plan in data["plans"]]
        buttons = confirmations.receipt_buttons(action_id, months, receipt.get("months"), bool(receipt.get("benefit")))
        left = data.get("pending", 1) - 1
        return ToolResult(
            card,
            f"{seen} La tarjeta salió al chat con {'la foto y ' if files else ''}los botones; quedan {left} pendientes más. Tú no "
            "apruebas ni rechazas: espera a que el administrador pulse. No repitas la tarjeta.",
            files=files,
            buttons=buttons,
        )

    async def _propose_rejection(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        if self._confirmations is None:
            return ToolResult(None, "No puedo proponer acciones en esta instalación.", is_error=True)
        receipt_id = _integer(args, "comprobante", 1, 2**31 - 1)
        reason = _text(args, "motivo", 10, 300).strip()
        data = await self._api.get(f"/assistant/receipts/{receipt_id}", telegram_user_id=telegram_user_id)
        if data["receipt"]["status"] != "pending":
            return ToolResult(None, f"El comprobante #{receipt_id} ya no está pendiente.")
        summary = f"Rechazar el comprobante #{receipt_id} de {data['athlete']['name']}.\nMotivo que recibirá por correo: «{reason}»"
        payload = {"receipt": receipt_id, "decision": {"accion": "rechazar", "motivo": reason}}
        action_id = await self._confirmations.propose(telegram_user_id, "receipt_reject", payload, summary)
        return ToolResult(
            summary,
            "Propuesta enviada al chat con los botones Rechazar y Cancelar. No está rechazado hasta que pulsen.",
            buttons=confirmations.buttons(action_id, "Rechazar"),
        )

    async def _propose_write(
        self, telegram_user_id: int, summary: str, path: str, body: dict[str, Any], done: str, confirm: str, to_model: str
    ) -> ToolResult:
        """Show what would be written, with its buttons. The call is stored as shown; the click sends it."""
        if self._confirmations is None:
            return ToolResult(None, "No puedo proponer acciones en esta instalación.", is_error=True)
        payload = {"path": path, "json": body, "done": done}
        action_id = await self._confirmations.propose(telegram_user_id, "member_write", payload, summary)
        return ToolResult(
            summary,
            f"{to_model} Propuesta enviada al chat con los botones {confirm} y Cancelar. No está hecho hasta que pulsen.".strip(),
            buttons=confirmations.buttons(action_id, confirm),
        )

    async def _propose_renewal(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        months = args.get("meses")
        if months not in (1, 3, 6) or isinstance(months, bool):
            raise ValueError("`meses` must be 1, 3 or 6")
        amount = args.get("monto")
        if amount is not None:
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not 0 <= amount <= 1_000_000:
                raise ValueError("`monto` must be an amount in pesos")
            amount = float(amount)
        paid = _iso_date(args, "fecha_pago")
        reference = args.get("referencia")
        if reference is not None:
            reference = _text(args, "referencia", 2, 120)
        athlete_id = await self._pick(args, telegram_user_id, names)
        if isinstance(athlete_id, ToolResult):
            return athlete_id
        data = await self._api.get(f"/assistant/athletes/{athlete_id}/renewal", telegram_user_id=telegram_user_id)
        if data.get("pending_receipt"):
            return ToolResult(
                None,
                f"No se puede: tiene el comprobante #{data['pending_receipt']} en revisión. Ese pago se aprueba con "
                "`revisar_comprobante`, no se registra dos veces.",
            )
        plan = next(p for p in data["plans"] if p["months"] == months)
        label = "1 mes" if months == 1 else f"{months} meses"
        lines = [f"Renovar la membresía de {data['athlete']['name']}: {label}", f"Precio del plan: {_money(plan['price'])}"]
        if amount is not None:
            lines.append(f"Monto recibido: {_money(amount)}" + ("" if abs(amount - plan["price"]) < 0.005 else " (no coincide con el plan)"))
        if paid:
            lines.append(f"Fecha de pago: {_day_year(paid)}")
        if reference:
            lines.append(f"Referencia: {reference}")
        lines.append(f"Hoy: {_coverage(data['membership'])}")
        lines.append(f"Quedaría cubierto hasta {_day_year(plan['covers_until'])}")
        body: dict[str, Any] = {"months": months}
        body.update({k: v for k, v in (("amount", amount), ("paid", paid), ("reference", reference)) if v is not None})
        return await self._propose_write(
            telegram_user_id, "\n".join(lines), f"/assistant/athletes/{athlete_id}/renew", body, f"Renovada ({label})", "Renovar",
            f"Plan de {label}: {_money(plan['price'])}; quedaría cubierto hasta {plan['covers_until']}.",
        )

    async def _propose_access(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        action = args.get("accion")
        if action not in ("pausar", "reactivar"):
            raise ValueError("`accion` must be pausar or reactivar")
        athlete_id = await self._pick(args, telegram_user_id, names)
        if isinstance(athlete_id, ToolResult):
            return athlete_id
        profile = (await self._api.get(f"/assistant/athletes/{athlete_id}/profile", telegram_user_id=telegram_user_id))["athlete"]
        if profile.get("archived"):
            return ToolResult(None, "Está archivado: eso se maneja en la consola.")
        pause = action == "pausar"
        if profile.get("active", True) != pause:
            return ToolResult(None, "Ya está pausado." if pause else "Ya está activo.")
        summary = (
            f"Pausar a {profile['name']}.\nDeja de poder entrar a la app hasta que se reactive."
            if pause
            else f"Reactivar a {profile['name']}.\nVuelve a poder entrar a la app."
        )
        return await self._propose_write(
            telegram_user_id, summary, f"/assistant/athletes/{athlete_id}/access",
            {"action": "pause" if pause else "reactivate"}, "Pausado" if pause else "Reactivado",
            "Pausar" if pause else "Reactivar", "",
        )

    async def _propose_race_time(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        seconds = _seconds(str(args.get("tiempo") or ""))
        wanted = _fold(_text(args, "evento")).split()
        athlete_id = await self._pick(args, telegram_user_id, names)
        if isinstance(athlete_id, ToolResult):
            return athlete_id
        data = await self._api.get(f"/assistant/athletes/{athlete_id}/events", telegram_user_id=telegram_user_id)
        hits = [e for e in data["events"] if all(w in _fold(e["name"]) for w in wanted)]
        if len(hits) != 1:
            listed = "; ".join(f"{e['name']} ({e['date']})" for e in data["events"]) or "ninguno"
            what = "varios eventos coinciden" if hits else "no está inscrito en un evento con ese nombre"
            return ToolResult(None, f"No propuse nada: {what}. Está inscrito en: {listed}. Pregunta cuál.")
        event = hits[0]
        lines = [
            f"Registrar {_clock(seconds)} a {data['athlete']['name']}",
            f"Evento: {event['name']} ({_day_year(event['date'])})" if event.get("date") else f"Evento: {event['name']}",
        ]
        if event.get("goal"):
            lines.append(f"Objetivo: {event['goal']}")
        if event.get("result_sec"):
            lines.append(f"Ya tenía registrado {_clock(event['result_sec'])}: se reemplaza.")
        return await self._propose_write(
            telegram_user_id, "\n".join(lines), f"/assistant/athletes/{athlete_id}/race-time",
            {"event_id": event["id"], "seconds": seconds}, f"Tiempo registrado ({_clock(seconds)})", "Registrar",
            f"Evento: {event['name']} ({event.get('date')}).",
        )

    async def _applications(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        status = args.get("situacion") or "pendiente"
        if status not in APPLICATION_STATUSES:
            raise ValueError("`situacion` must be pendiente, espera or rechazada")
        layout = _format(args)
        data = await self._api.get(
            "/assistant/applications", telegram_user_id=telegram_user_id, params={"status": APPLICATION_STATUSES[status]}
        )
        found = data["applications"]
        what = {"pendiente": "pendiente(s)", "espera": "en lista de espera", "rechazada": "rechazada(s)"}[status]
        if not found:
            return ToolResult(None, f"Ninguna solicitud {what}.")
        names = names if names is not None else Pseudonyms()
        to_model = f"{data['total']} solicitud(es) {what}, de la más antigua a la más reciente.\n" + "\n".join(
            f"{names.code(a['id'], a['name'])} | " + " | ".join(_application_facts(a)) for a in found
        )

        def answered(a: dict[str, Any]) -> str:
            return "contestado" if a["questionnaire"] else "sin contestar"

        def when(a: dict[str, Any], show: Any) -> str:
            return show(a["requested"]) if a.get("requested") else ""

        return await self._deliver(
            layout,
            "solicitudes",
            "Solicitudes",
            f"{data['total']} {what}" if data["total"] != 1 else f"1 {what}",
            "De la más antigua a la más reciente",
            APPLICATION_COLUMNS,
            [
                [when(a, _day_year) or charts.EMPTY_CELL, _clip(a["name"], NAME_CELL), _clip(a.get("city") or charts.EMPTY_CELL, 18), answered(a)]
                for a in found
            ],
            ["Fecha", "Nombre", "Ciudad", "Género", "Edad aprox.", "Cuestionario", "Comentario"],
            [
                [when(a, str), a["name"], a.get("city") or "", a.get("gender") or "", a.get("age") or "", answered(a), a.get("comment") or ""]
                for a in found
            ],
            to_model,
        )

    async def _review_application(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        names = names if names is not None else Pseudonyms()

        async def listed(status: str) -> list[dict[str, Any]]:
            data = await self._api.get("/assistant/applications", telegram_user_id=telegram_user_id, params={"status": status})
            return data["applications"]

        if args.get("nombre"):
            name = _text(args, "nombre")
            known = names.athlete_id(name)
            if known is not None:
                application = (
                    await self._api.get(f"/assistant/applications/{known}", telegram_user_id=telegram_user_id)
                )["application"]
            else:
                words = _fold(name).split()
                hits = [
                    a for status in ("pending", "waiting", "rejected") for a in await listed(status)
                    if all(w in _fold(a["name"]) for w in words)
                ]
                hits = list({a["id"]: a for a in hits}.values())
                if not hits:
                    return ToolResult("No encontré una solicitud abierta con ese nombre.", "Sin resultados; ya se lo dije al administrador.")
                if len(hits) > 1:
                    labels = [f"{a['name']} · {APPLICATION_STATE.get(a['status'], a['status'])}" for a in hits]
                    unique = len(hits) <= MAX_CHOICES and len(set(labels)) == len(labels)
                    return ToolResult(
                        "Hay varias solicitudes con ese nombre. ¿Cuál?\n" + "\n".join(f"- {label}" for label in labels),
                        f"Hay {len(hits)} solicitudes con ese nombre; ya le pregunté al administrador cuál. Espera su respuesta.",
                        buttons=choice_buttons([_clip(label, CHOICE_LABEL_MAX) for label in labels], telegram_user_id) if unique else None,
                    )
                application = hits[0]
        else:
            queue = await listed("pending")
            if not queue:
                return ToolResult(None, "No hay solicitudes pendientes.")
            # The oldest one that can be decided; the ones without a questionnaire only when there is nothing else.
            application = next((a for a in queue if a["questionnaire"]), queue[0])

        card = application_card(application)
        code = names.code(application["id"], application["name"])
        seen = f"Solicitud de {code}: " + " | ".join(_application_facts(application)) + "."
        if application.get("comment"):
            seen += " Su comentario (texto que escribió) salió al chat; tú no lo ves."
        if self._confirmations is None:
            return ToolResult(card, seen + " Salió al chat sin botones.")
        action_id = await self._confirmations.propose(telegram_user_id, "application", {"athlete": application["id"]}, card)
        return ToolResult(
            card,
            seen + " La tarjeta salió al chat con los botones. Tú no decides: espera a que el administrador pulse. No "
            "repitas la tarjeta.",
            buttons=confirmations.application_buttons(action_id, application["questionnaire"]),
        )

    async def _garmin_errors(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        start, end = _iso_date(args, "desde"), _iso_date(args, "hasta")
        if start and end and start > end:
            raise ValueError("`desde` is after `hasta`")
        layout = _format(args)
        params = {key: value for key, value in (("from", start), ("to", end)) if value}
        data = await self._api.get("/assistant/garmin/errors", telegram_user_id=telegram_user_id, params=params)
        period = _period(data["period"]["from"], data["period"]["to"])
        workouts = data["workouts"]
        if not workouts:
            return ToolResult(None, f"Ningún entreno rechazado por Garmin con fecha {period}.")
        names = names if names is not None else Pseudonyms()

        def state(w: dict[str, Any]) -> str:
            return "ya no se intenta" if w["gave_up"] else "se reintenta"

        reasons = "\n".join(
            f"- {r['workouts']} entreno(s) de {r['athletes']} atleta(s): {_clip(r['error'], 160)}" for r in data["reasons"]
        )
        left = f" Van solo los primeros {len(workouts)}." if data.get("truncated") else ""
        to_model = (
            f"{data['total']} entreno(s) de {data['athletes']} atleta(s) sin llegar al reloj, con fecha {period}.{left}\n"
            f"Motivos:\n{reasons}\nFilas (atleta | fecha | tipo | intentos | estado):\n"
            + "\n".join(
                f"{names.code(w['athlete']['id'], w['athlete']['name'])} | {w['date']} | {w['type'] or '?'} | "
                f"{w['attempts']} | {state(w)}"
                for w in workouts
            )
        )
        return await self._deliver(
            layout,
            "errores_garmin",
            "Garmin",
            f"{data['total']} entrenos sin llegar al reloj" if data["total"] != 1 else "1 entreno sin llegar al reloj",
            f"{data['athletes']} atleta(s)  ·  {period}",
            GARMIN_COLUMNS,
            [
                [
                    _day(w["date"]) if w["date"] else charts.EMPTY_CELL, _clip(w["athlete"]["name"], NAME_CELL),
                    _clip(w["type"] or charts.EMPTY_CELL, 20), state(w), str(w["attempts"]),
                ]
                for w in workouts
            ],
            ["Fecha", "Atleta", "Tipo", "Intentos", "Estado", "Error"],
            [[w["date"] or "", w["athlete"]["name"], w["type"] or "", w["attempts"], state(w), w["error"]] for w in workouts],
            to_model,
        )

    async def _sent_messages(self, args: dict[str, Any], telegram_user_id: int) -> ToolResult:
        start, end = _iso_date(args, "desde"), _iso_date(args, "hasta")
        if start and end and start > end:
            raise ValueError("`desde` is after `hasta`")
        layout = _format(args)
        params = {key: value for key, value in (("from", start), ("to", end)) if value}
        data = await self._api.get("/assistant/messages", telegram_user_id=telegram_user_id, params=params)
        period = _period(data["period"]["from"], data["period"]["to"])
        sent = data["messages"]
        if not sent:
            return ToolResult(None, f"Ningún aviso enviado {period}.")
        left = f" Van solo los {len(sent)} más recientes." if data.get("truncated") else ""

        def channel(m: dict[str, Any]) -> str:
            return CHANNEL_NAMES.get(m["channel"], "?")

        to_model = (
            f"{data['total']} aviso(s) enviados {period}.{left} El asunto salió al chat; tú no lo ves.\n"
            "Filas (fecha | canal | categoría | destinatarios | aceptados | fallidos | sin destino | omitidos):\n"
            + "\n".join(
                f"{m['date']} | {channel(m)} | {m['category'] or '?'} | " + " | ".join(_delivery_cells(m))
                + f" | {m['delivery']['skipped']}"
                for m in sent
            )
        )
        return await self._deliver(
            layout,
            "avisos",
            "Avisos",
            f"{data['total']} avisos enviados" if data["total"] != 1 else "1 aviso enviado",
            period + (f"  ·  los {len(sent)} más recientes" if data.get("truncated") else ""),
            MESSAGE_COLUMNS,
            [[_day(m["date"]) if m["date"] else charts.EMPTY_CELL, channel(m), _clip(m["subject"], NAME_CELL), *_delivery_cells(m)] for m in sent],
            ["Fecha", "Canal", "Categoría", "Asunto", "Destinatarios", "Aceptados", "Fallidos", "Sin destino", "Omitidos"],
            [
                [m["date"] or "", channel(m), m["category"] or "", m["subject"], *_delivery_cells(m), m["delivery"]["skipped"]]
                for m in sent
            ],
            to_model,
        )

    async def _propose_announcement(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        if self._confirmations is None:
            return ToolResult(None, "No puedo proponer acciones en esta instalación.", is_error=True)
        subject = _text(args, "asunto", 3, SUBJECT_MAX)
        message = _text(args, "mensaje", 5, ANNOUNCEMENT_MAX)
        channel = args.get("canal")
        if channel not in CHANNELS:
            raise ValueError("`canal` must be correo, push or ambos")
        everyone = args.get("todos") is True
        filters = _filters(args)
        codes = args.get("atletas") or []
        if not isinstance(codes, list) or len(codes) > 200:
            raise ValueError("`atletas` must be a list of at most 200 ATLETA_NN codes")
        ids = []
        for code in codes:
            known = names.athlete_id(str(code)) if names else None
            if known is None:
                raise ValueError(f"I do not know {code!r}: look the person up with `buscar_atleta` first")
            ids.append(known)
        if everyone and (filters or ids):
            raise ValueError("`todos` goes alone: drop it, or drop `filtros` and `atletas`")
        if not (everyone or filters or ids):
            raise ValueError("Say who it is for: `todos`, `filtros` or `atletas`. If the admin did not say, ask.")

        body: dict[str, Any] = {"filters": filters, "athlete_ids": ids, "everyone": everyone}
        found = await self._filtered("/assistant/messages/preview", telegram_user_id, body)
        if found.get("notes"):
            # A name that matched nothing: sending to the rest would not be what was asked.
            return ToolResult(None, "No propuse nada." + _understood(found) + " Pregunta al administrador cuál quiso decir.")
        audience = found["athletes"]
        if not audience:
            return ToolResult(None, "Nadie activo cumple eso: no hay a quién mandarlo." + _understood(found))

        category = None
        wanted = args.get("categoria")
        if isinstance(wanted, str) and wanted.strip():
            hits = [c for c in found["categories"] if _fold(wanted.strip()) in _fold(c["name"] or "")]
            if len(hits) != 1:
                options = ", ".join(c["name"] or "?" for c in found["categories"])
                raise ValueError(f"No single category matches {wanted!r}. The categories are: {options}")
            category = hits[0]

        total, reach = found["total"], found["reach"]
        via = {"email": "correo", "push": "push", "both": "correo y push"}[CHANNELS[channel]]
        lines = [f"Aviso por {via} a {total} atleta(s) activo(s) · {_audience_line(found, everyone, len(ids))}"]
        if CHANNELS[channel] != "email":
            lines.append(f"Push: {reach['push']} con la app en su teléfono; {total - reach['push']} no lo recibirán por ahí.")
        if CHANNELS[channel] != "push":
            lines.append(f"Correo: {reach['email']} con dirección.")
        if found.get("not_active"):
            lines.append(f"{found['not_active']} de los nombrados no están activos y no lo reciben.")
        if total <= NAMES_ON_CARD:
            lines.append("Para: " + ", ".join(a["name"] for a in audience) + ".")
        if category:
            lines.append(f"Categoría: {category['name']}.")
        lines += ["", f"Asunto: {subject}", "", message]
        summary = "\n".join(lines)

        payload: dict[str, Any] = {
            "user_ids": [a["id"] for a in audience], "subject": subject, "message": message, "channel": CHANNELS[channel],
        }
        if category:
            payload["category_id"] = category["id"]
        action_id = await self._confirmations.propose(telegram_user_id, "announcement", payload, summary)
        listing = table_csv(
            ["Nombre", "Grupo", "Push", "Correo"],
            [[a["name"], a.get("group") or "", "sí" if a["push"] else "no", "sí" if a["email"] else "no"] for a in audience],
        )
        where = "La lista salió como archivo." if total > NAMES_ON_CARD else "Los nombres van en la tarjeta."
        return ToolResult(
            summary,
            f"Propuesta enviada al chat con los botones Enviar y Cancelar: {total} atleta(s) activo(s), {reach['push']} con "
            f"push y {reach['email']} con correo. {where} No está enviado hasta que pulsen."
            + _understood(found),
            files=[OutFile("destinatarios.csv", listing)] if total > NAMES_ON_CARD else [],
            buttons=confirmations.buttons(action_id, "Enviar"),
        )

    async def _profile(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        athlete_id = await self._pick(args, telegram_user_id, names)
        if isinstance(athlete_id, ToolResult):
            return athlete_id
        data = await self._api.get(f"/assistant/athletes/{athlete_id}/profile", telegram_user_id=telegram_user_id)
        facts = _profile_facts(data)
        athlete = data["athlete"]
        shown = [athlete["name"] + ("" if athlete.get("active", True) else " (inactivo)"), *facts]
        goal = data.get("goal")
        if goal:
            shown.append("Meta: " + " · ".join(str(v) for v in (goal.get("distance"), goal.get("time"), goal.get("date")) if v))
        hidden = " Su meta (texto que escribió el atleta) salió al chat; tú no la ves." if goal else ""
        return ToolResult("\n".join(shown), "Ficha enviada al chat. " + " | ".join(facts) + "." + hidden)

    async def _tables(
        self,
        filename: str,
        label: str,
        title: str,
        subtitle: str,
        columns: list[tuple[str, float, str]],
        rows: list[list[str]],
        to_model: str,
        colors: Optional[dict[tuple[int, int], str]] = None,
    ) -> ToolResult:
        """The rows as one picture, or as an album when one cannot hold them. `colors` paints cells of a one-page table."""
        pages = _pages(rows)
        files = []
        for n, page in enumerate(pages, 1):
            mark = f"  ·  {n} de {len(pages)}" if len(pages) > 1 else ""
            png = await asyncio.to_thread(charts.table_png, label, title, subtitle + mark, columns, page, colors if len(pages) == 1 else None)
            suffix = f"_{n}" if len(pages) > 1 else ""
            files.append(OutFile(f"{filename}{suffix}.png", png, photo=True))
        what = "La tabla ya salió al chat como imagen" if len(pages) == 1 else f"La tabla ya salió al chat en {len(pages)} imágenes"
        return ToolResult("", to_model + "\n" + SENT.format(what=what), files=files)

    async def _laps(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        workout_id = _integer(args, "entreno", 1, 2**31 - 1)
        layout = _format(args)
        athlete_id = await self._pick(args, telegram_user_id, names)
        if isinstance(athlete_id, ToolResult):
            return athlete_id

        data = await self._api.get(
            f"/assistant/athletes/{athlete_id}/workouts/{workout_id}/laps", telegram_user_id=telegram_user_id
        )
        laps = data["laps"]
        if not laps:
            return ToolResult(None, f"El entreno #{workout_id} del {data['date']} no tiene vueltas registradas.")
        kept = f" Van solo las primeras {len(laps)}." if data.get("truncated") else ""
        to_model = (
            f"Entreno #{workout_id} del {data['date']}: {data['total']} vuelta(s).{kept}\n"
            + "\n".join(_lap_row(lap) for lap in laps)
        )
        name = (data.get("athlete") or {}).get("name")
        if layout == "csv" or (layout == "auto" and len(laps) > charts.TABLE_MAX_ROWS):
            rows = [[lap["lap"], f"{lap['distance_m']:.0f}" if lap.get("distance_m") else "", *_csv_cells(lap)] for lap in laps]
            caption = f"{name + ': ' if name else ''}{len(laps)} vueltas del {_day_year(data['date'])}."
            return ToolResult(
                caption,
                to_model + "\n" + SENT.format(what="La lista ya salió al chat como archivo CSV"),
                files=[OutFile("vueltas.csv", table_csv(LAP_CSV, rows))],
            )
        whole = data.get("workout") or {}
        totals = [f"{whole['distance_km']:.2f} km"] if whole.get("distance_km") else []
        totals += [c for c in _cells(whole)[:1] if c != charts.EMPTY_CELL]
        totals += [f"{whole['pace']} min/km"] if whole.get("pace") else []
        totals += [f"FC {whole['heart_rate']:.0f} lpm"] if whole.get("heart_rate") else []
        return await self._tables(
            "vueltas",
            "Vueltas",
            f"{name} · {_day_year(data['date'])}" if name else _day_year(data["date"]),
            "  ·  ".join(totals) or f"{data['total']} vueltas",
            LAP_COLUMNS,
            [[str(lap["lap"]), _distance(lap.get("distance_m")), *_cells(lap)] for lap in laps],
            to_model,
        )

    async def _summary(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms] = None) -> ToolResult:
        start, end = _iso_date(args, "desde"), _iso_date(args, "hasta")
        athlete_id = await self._pick(args, telegram_user_id, names)
        if isinstance(athlete_id, ToolResult):
            return athlete_id

        params: dict[str, Any] = {}
        if start:
            params["from"] = start
        if end:
            params["to"] = end
        if args.get("ciclo"):
            params["use_cycle"] = "true"
        data = await self._api.get(
            f"/assistant/athletes/{athlete_id}/summary", telegram_user_id=telegram_user_id, params=params
        )
        done, prescribed = data["workouts"]["done"], data["workouts"]["prescribed"]
        return ToolResult(render_summary(data), f"Resumen enviado al chat: {done} de {prescribed} entrenos.")
