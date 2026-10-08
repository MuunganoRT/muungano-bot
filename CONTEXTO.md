# Duma (muungano-bot): qué hace hoy

Estado de lo **implementado y desplegado**. Lo que falta y las decisiones de diseño viven en [PLAN.md](PLAN.md).
Se actualiza en cada commit: si el código cambia lo que dice este archivo, el mismo commit lo corrige.

Última actualización: 2026-10-07, commit `3b5fbb3`.

## Qué es

Bot de Telegram para los administradores de Muungano. Contesta preguntas sobre atletas, entrenos y pagos, y deja
aprobar o rechazar comprobantes con botones. Corre en el Cloudron de Muungano como el programa `bot` de supervisor
(`/app/data/bot`, checkout de `main`); el CI corre las pruebas y despliega al hacer push.

| Pieza | Dónde |
|---|---|
| Arranque, mensajes, comandos, clics | `duma/main.py` |
| Ciclo con el modelo, sesiones, compactación | `duma/agent.py`, `duma/sessions.py` |
| Herramientas del modelo | `duma/tools.py` |
| Cliente del API y sus listas de rutas | `duma/api_client.py` |
| Texto, CSV e imágenes | `duma/render.py`, `duma/charts.py` |
| Propuestas con botones | `duma/confirmations.py` |
| Instrucciones del modelo | `prompts/system.md` |

## Quién puede hablarle y dónde

- Solo en el grupo `TELEGRAM_ADMIN_CHAT_ID` y solo los ids de `TELEGRAM_ALLOWED_USER_IDS` (`duma/auth.py`).
- En **General** hay que usar `/ruun <pregunta>`: abre un tema nuevo y contesta ahí. Un mensaje suelto en General
  recibe un recordatorio.
- En **cualquier otro tema**, también uno creado a mano, contesta sin comando. Cada tema es una sesión aparte.
- Entra texto, imágenes, PDF, CSV o texto, y notas de voz (transcritas en el servidor, `duma/voice.py`).
- Comandos: `/ruun`, `/usage`, `/clear`, `/prefs`, `/forget N`, `/help`.

## Qué puede consultar

Todo sale de las rutas `/assistant/*` del API (lista cerrada `ALLOWED` en `duma/api_client.py`). No tiene SQL.

| Herramienta | Contesta |
|---|---|
| `buscar_atleta` | quién es, por nombre; pregunta si hay homónimos |
| `resumen_atleta` | hechos contra prescritos, score, km, ritmo y la tirada más larga de un periodo o ciclo |
| `entrenos_atleta`, `vueltas_entreno` | cada entreno hecho y el desglose por vuelta de uno |
| `plan_atleta` | qué le toca y qué no hizo, día por día; admite fechas futuras |
| `pagos_atleta` | hasta cuándo está cubierto y sus comprobantes |
| `perfil_atleta` | nivel, sede, género, edad aproximada, reloj, meta, evento principal |
| `comprobantes` | la cola de pendientes, los aprobados o los rechazados |
| `buscar_atletas` | lista de quienes cumplen unos filtros |
| `cifras` | totales de un conjunto: personas, pagos, entrenos, km, score |
| `consultar` | una fila por persona para comparar o razonar (tope de 60) |
| `grafica` | por semana, ranking o dispersión del score, como imagen |
| `catalogo`, `preguntar` | nombres de grupos y eventos; una pregunta al admin con botones |

**Filtros** de `buscar_atletas`, `cifras`, `consultar` y `grafica`, combinables: evento, pago en un periodo, grupo
(uno o varios), entrenos hechos, membresía (vigente, vencida, sin membresía, vence en un periodo), comprobante
(pendiente, aprobado, rechazado; beneficio o pago), perfil (nivel, sede, género, con o sin reloj) y faltas (entrenos
prescritos sin hacer).

No tiene: correo, teléfono ni contacto de nadie; el título y la descripción que el coach escribe en un entreno;
errores de Garmin; avisos enviados; convenios.

## Qué puede escribir

Una sola cosa: **decidir un comprobante**.

- `revisar_comprobante` manda la foto, una tarjeta (plan pedido, monto leído contra el esperado, cobertura) y los
  botones: un plan por botón, un motivo de rechazo por botón y «Dejar pendiente».
- `rechazar_comprobante` propone un rechazo con un motivo propio, con «Rechazar» y «Cancelar».
- **El modelo no escribe.** La ruta `POST /assistant/receipts/{id}/decide` está en `WRITES` y solo la llama el
  manejador del botón (`Bot._decide_receipt`, `duma/main.py`).
- La tarjeta es de un solo uso, caduca (`BOT_CONFIRM_TTL_MIN`) y solo la decide quien la pidió.
- El API registra la decisión como el usuario de consola ligado al Telegram de quien pulsó. Hoy solo hay uno
  (`ASSISTANT_ADMINS` en `muungano-api/security.py`): a cualquier otro el API le contesta 403.
- Aprobar hace lo mismo que la consola: activa la membresía, manda correo al atleta y publica sus entrenos en Garmin.

También guarda **preferencias permanentes** del equipo (`duma/preferences.py`), igual con botón de confirmar.

## Cómo salen las respuestas

- Texto plano: Telegram no interpreta Markdown aquí.
- Tablas (entrenos, vueltas, plan, pagos, comprobantes): imagen hasta 40 filas, CSV si son más, o un álbum de
  imágenes repartidas parejo si el admin pide imagen (`Toolbox._deliver` y `_tables`, `duma/tools.py`).
- Gráficas como imagen (`duma/charts.py`).

## Privacidad

- El modelo nunca recibe nombres: ve códigos `ATLETA_NN` (`duma/pseudonyms.py`) y el bot pone el nombre al escribir
  en el chat. Los nombres de grupos y eventos sí los ve.
- Lo que escribió una persona (meta del cuestionario, motivo de rechazo, referencia leída de un comprobante) sale al
  chat o al CSV, no al modelo.
- La foto de un comprobante va del API al chat sin pasar por el modelo.

## Sesiones y costo

- Una sesión por tema, guardada en Postgres (esquema `duma`). Se compacta sola al pasar `BOT_SESSION_MAX_TOKENS` y
  se cierra tras `BOT_SESSION_IDLE_HOURS` sin mensajes (0 = nunca).
- Si cambian las instrucciones o las herramientas, la sesión descarta sus bloques de razonamiento antes del
  siguiente mensaje (`Agent._align`, `duma/agent.py`).
- Tope de gasto diario `BOT_DAILY_BUDGET_USD`; `/usage` muestra el costo del tema.
- `state/audit.log` registra cada herramienta llamada y cada clic, con quién lo hizo.

## Pruebas

`.venv/bin/python -m pytest -q` (278 pruebas). El CI las corre y, si pasan, llama a `/deploy/bot`. No hay lint en CI.
