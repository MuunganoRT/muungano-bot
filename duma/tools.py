"""The tools Duma's model can call.

Two kinds. "Direct" tools (`buscar_atleta`, `resumen_atleta`, `buscar_atletas`,
`grafica`) send what they find to the chat as text, a file built by `render` or
a chart drawn by `charts`, and the model gets back only a short acknowledgement
with no names in it. `cifras`, `catalogo` and `consultar` answer the model:
with totals, which identify nobody; with the names of groups and events; and
with one row per person where the name is a code. `entrenos_atleta` and
`vueltas_entreno` do both: the chat gets a table drawn by `charts`, with the
athlete's name on it, and the model gets the same figures with no name at
all. Either way the model never reads a person's name
it was not given by the admin.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from duma import charts, confirmations
from duma.api_client import ApiError, MuunganoApi
from duma.confirmations import Confirmations
from duma.preferences import PreferenceError, Preferences, clean
from duma.pseudonyms import Pseudonyms
from duma.render import _day, _day_year, _num, _period, _race_time, matches_csv, render_candidates, render_matches, render_summary

log = logging.getLogger(__name__)


@dataclass
class OutFile:
    name: str
    content: bytes
    # Shown in the chat as a picture instead of attached as a download.
    photo: bool = False


@dataclass
class ToolResult:
    # Written to the chat as is. None when the tool has nothing to show.
    direct_text: Optional[str]
    # What the model sees. Never contains athlete names.
    to_model: str
    is_error: bool = False
    # Attached to the chat with `direct_text` as its caption. The model never sees it.
    file: Optional[OutFile] = None
    # (label, callback data) pairs shown under `direct_text`: a proposal waiting for an admin's click.
    buttons: Optional[list[tuple[str, str]]] = None


FILTERS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "maxItems": 8,
    "items": {
        "type": "object",
        "properties": {
            "tipo": {"type": "string", "enum": ["evento", "pago", "grupo", "entrenos"]},
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
            "desde": {"type": "string", "description": "Para `pago` y `entrenos`: YYYY-MM-DD."},
            "hasta": {"type": "string", "description": "Para `pago` y `entrenos`: YYYY-MM-DD."},
            "minimo": {"type": "integer", "description": "Solo `entrenos`: mínimo de entrenos hechos."},
        },
        "required": ["tipo"],
    },
}

SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "buscar_atleta",
        "description": (
            "Busca personas del equipo por nombre (atletas, coaches y admins) y muestra la lista en el chat. Úsala "
            "cuando el administrador quiera saber quién es alguien o cuántos se llaman así, sin pedir su resumen."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "texto": {"type": "string", "description": "Parte del nombre o apellido, mínimo 2 letras."},
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
            "Manda al chat una tabla (imagen) con los entrenos que hizo un atleta y te devuelve a ti las mismas "
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
            },
            "required": ["nombre"],
        },
    },
    {
        "name": "vueltas_entreno",
        "description": (
            "Manda al chat una tabla (imagen) con el desglose por vuelta (lap) de un entreno y te devuelve a ti las "
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
            },
            "required": ["nombre", "entreno"],
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
    raise ValueError("`tipo` must be evento, pago, grupo or entrenos")


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
        parts.append(f"evento {e['event']} ({e['date']}){result}")
    payment = person.get("last_payment")
    if payment:
        amount = f" ${payment['amount']:,.2f}" if payment.get("amount") is not None else " sin monto"
        parts.append(f"último pago {payment['date']}{amount}")
    if summary is not None:
        workouts = summary["workouts"]
        parts.append(f"entrenos {workouts['done']}/{workouts['prescribed']}")
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
LAP_COLUMNS = [
    ("Vuelta", 0.7, "left"), ("Distancia", 1.4, "right"), ("Tiempo", 1.5, "right"),
    ("Ritmo", 1.3, "right"), ("FC", 1.2, "right"), ("Score", 1.2, "right"),
]
TABLE_SENT = " La tabla ya salió al chat como imagen: no repitas sus cifras; comenta en dos o tres líneas lo que importa."


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

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    async def run(
        self, name: str, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms] = None
    ) -> ToolResult:
        names = names if names is not None else Pseudonyms()
        handlers = {
            "buscar_atleta": self._search,
            "resumen_atleta": self._summary,
            "buscar_atletas": self._query,
            "cifras": self._aggregate,
            "catalogo": self._catalog,
            "preguntar": self._ask,
            "grafica": lambda a, u: self._chart(a, u, names),
            "consultar": lambda a, u: self._analyze(a, u, names),
            "entrenos_atleta": lambda a, u: self._workouts(a, u, names),
            "vueltas_entreno": lambda a, u: self._laps(a, u, names),
        }
        handlers["resumen_atleta"] = lambda a, u: self._summary(a, u, names)
        if self._confirmations and self._preferences:
            handlers["guardar_preferencia"] = self._propose_preference
        handler = handlers.get(name)
        if handler is None:
            return ToolResult(None, f"Unknown tool {name!r}", is_error=True)
        try:
            return await handler(args, telegram_user_id)
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

    async def _search(self, args: dict[str, Any], telegram_user_id: int) -> ToolResult:
        found = await self._find(_text(args, "texto"), _member_status(args), telegram_user_id)
        athletes, total = found["athletes"], found["total"]
        return ToolResult(
            render_candidates(athletes, total, question=False),
            f"{total} resultado(s); ya los mostré en el chat.",
        )

    async def _query(self, args: dict[str, Any], telegram_user_id: int) -> ToolResult:
        body: dict[str, Any] = {"filters": _filters(args), "member_status": _member_status(args)}
        count_only = args.get("solo_contar") is True
        if count_only:
            body["count_only"] = True
        else:
            body["limit"] = QUERY_LIMIT
        found = await self._api.post("/assistant/athletes/query", telegram_user_id=telegram_user_id, json=body)

        total = found["total"]
        if count_only:
            return ToolResult(None, f"{total} persona(s) cumplen los filtros.{_understood(found)}")
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
            file=OutFile("atletas.csv", matches_csv(found)),
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
        found = await self._api.post("/assistant/athletes/aggregate", telegram_user_id=telegram_user_id, json=body)
        return ToolResult(None, _figures(found) + _understood(found))

    async def _analyze(self, args: dict[str, Any], telegram_user_id: int, names: Pseudonyms) -> ToolResult:
        start, end = _iso_date(args, "desde"), _iso_date(args, "hasta")
        if bool(start) != bool(end):
            raise ValueError("give both `desde` and `hasta`, or neither")
        if start and start > end:
            raise ValueError("`desde` is after `hasta`")
        cap = ANALYZE_ROWS_WITH_PERIOD if start else ANALYZE_ROWS
        body = {"filters": _filters(args), "member_status": _member_status(args), "limit": cap}
        found = await self._api.post("/assistant/athletes/query", telegram_user_id=telegram_user_id, json=body)
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

        found = await self._api.post("/assistant/athletes/series", telegram_user_id=telegram_user_id, json=body)
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
            return ToolResult("", acknowledgement, file=OutFile(f"ranking_{metric}.png", png, photo=True))

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
            return ToolResult("", acknowledgement, file=OutFile("dispersion_score.png", png, photo=True))

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
        return ToolResult("", acknowledgement, file=OutFile(f"{metric}.png", png, photo=True))

    async def _workouts(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        start, end = _iso_date(args, "desde"), _iso_date(args, "hasta")
        shortest, longest = _positive(args, "km_min"), _positive(args, "km_max")
        if shortest is not None and longest is not None and shortest > longest:
            raise ValueError("`km_min` is above `km_max`")
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
        shown = workouts[-charts.TABLE_MAX_ROWS :]
        count = f"{len(workouts)} entrenos" if len(shown) == len(workouts) else f"los últimos {len(shown)} de {len(workouts)}"
        png = await asyncio.to_thread(
            charts.table_png,
            "Entrenos",
            (data.get("athlete") or {}).get("name") or "Entrenos",
            f"{_period(data['period']['from'], data['period']['to'])}  ·  {count}",
            WORKOUT_COLUMNS,
            [[_day(w["date"]), w.get("type") or charts.EMPTY_CELL, f"{w['distance_km']:.1f}", *_cells(w)] for w in shown],
        )
        return ToolResult("", to_model + "\n" + TABLE_SENT.strip(), file=OutFile("entrenos.png", png, photo=True))

    async def _laps(self, args: dict[str, Any], telegram_user_id: int, names: Optional[Pseudonyms]) -> ToolResult:
        workout_id = _integer(args, "entreno", 1, 2**31 - 1)
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
        if len(laps) > charts.TABLE_MAX_ROWS:
            return ToolResult(None, to_model + "\nSon demasiadas vueltas para una tabla: resume tú lo que importa.")
        whole = data.get("workout") or {}
        totals = [f"{whole['distance_km']:.2f} km"] if whole.get("distance_km") else []
        totals += [c for c in _cells(whole)[:1] if c != charts.EMPTY_CELL]
        totals += [f"{whole['pace']} min/km"] if whole.get("pace") else []
        totals += [f"FC {whole['heart_rate']:.0f} lpm"] if whole.get("heart_rate") else []
        name = (data.get("athlete") or {}).get("name")
        png = await asyncio.to_thread(
            charts.table_png,
            "Vueltas",
            f"{name} · {_day_year(data['date'])}" if name else _day_year(data["date"]),
            "  ·  ".join(totals) or f"{data['total']} vueltas",
            LAP_COLUMNS,
            [[str(lap["lap"]), _distance(lap.get("distance_m")), *_cells(lap)] for lap in laps],
        )
        return ToolResult("", to_model + "\n" + TABLE_SENT.strip(), file=OutFile("vueltas.png", png, photo=True))

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
