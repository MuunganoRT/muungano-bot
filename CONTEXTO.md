# Duma (muungano-bot): qué hace hoy

Estado de lo **implementado y desplegado**. Lo que falta y las decisiones de diseño viven en [PLAN.md](PLAN.md).
Se actualiza en cada commit: si el código cambia lo que dice este archivo, el mismo commit lo corrige.

Última actualización: 2026-10-09.

## Qué es

Bot de Telegram para los administradores de Muungano. Contesta preguntas sobre atletas, entrenos y pagos, deja
aprobar o rechazar comprobantes, decidir solicitudes de ingreso y mandar avisos por correo y push, siempre con botones. Corre en el Cloudron de Muungano como el programa `bot` de supervisor
(`/app/data/bot`, checkout de `main`); el CI corre las pruebas y despliega al hacer push.

| Pieza | Dónde |
|---|---|
| Arranque, mensajes, comandos, clics | `duma/main.py` |
| Ciclo con el modelo, sesiones, compactación | `duma/agent.py`, `duma/sessions.py` |
| Herramientas del modelo | `duma/tools.py` |
| Cliente del API y sus listas de rutas | `duma/api_client.py` |
| Texto, CSV e imágenes | `duma/render.py`, `duma/charts.py` |
| Propuestas con botones | `duma/confirmations.py` |
| Avisos que manda el API | `duma/hooks.py` |
| Instrucciones del modelo | `prompts/system.md` |

## Quién puede hablarle y dónde

- Solo en el grupo `TELEGRAM_ADMIN_CHAT_ID` y solo los ids de `TELEGRAM_ALLOWED_USER_IDS` (`duma/auth.py`).
- En **General** se abre un tema con `/ruun`: solo, abre el tema «Nueva pregunta», contesta «Sí, dime» y el tema
  toma su nombre de lo primero que se pregunte ahí (los que esperan nombre se guardan en `state/untitled.topics`); con la pregunta junto (`/ruun <pregunta>`), abre el tema y
  contesta. `/run`, `/runn` y `/duma` hacen lo mismo (`RUN_ALIASES`, `duma/main.py`). Un mensaje suelto en General
  recibe un recordatorio.
- En **cualquier otro tema**, también uno creado a mano, contesta sin comando. Cada tema es una sesión aparte.
- Entra texto, imágenes, PDF, CSV o texto, y notas de voz (transcritas en el servidor, `duma/voice.py`).
- Comandos: `/ruun`, `/usage`, `/clear`, `/prefs`, `/forget N`, `/help`.

## Qué puede consultar

Todo sale de las rutas `/assistant/*` del API (lista cerrada `ALLOWED` en `duma/api_client.py`). No tiene SQL.

| Herramienta | Contesta |
|---|---|
| `buscar_atleta` | quién es, por nombre; pregunta si hay homónimos. Con filtros busca el nombre solo entre quienes los cumplen («Ari, la de Berlin») y le devuelve al modelo el código, sin mandar nada al chat |
| `resumen_atleta` | hechos contra prescritos, score, km, ritmo y la tirada más larga de un periodo o ciclo. Lo prescrito para hoy o después que aún no se hace sale como «por hacer» y no cuenta en el score |
| `entrenos_atleta`, `vueltas_entreno` | cada entreno hecho y el desglose por vuelta de uno |
| `plan_atleta` | qué le toca y qué no hizo, día por día; admite fechas futuras |
| `pagos_atleta` | hasta cuándo está cubierto y sus comprobantes |
| `perfil_atleta` | nivel, sede, género, edad aproximada, reloj, meta, evento principal |
| `comprobantes` | la cola de pendientes, los aprobados o los rechazados |
| `solicitudes` | quién pidió entrar y nadie ha aceptado: pendientes, en lista de espera o rechazadas |
| `errores_garmin` | entrenos que Garmin rechazó y no llegaron al reloj, con el motivo y si se sigue reintentando |
| `avisos_enviados` | avisos mandados por correo o push, con cuántos aceptó cada canal |
| `buscar_atletas` | lista de quienes cumplen unos filtros. Con solo un filtro de evento, la imagen «Registro a <carrera>»: atleta, grupo, tiempo objetivo, resultado y diferencia, en verde cuando el resultado iguala o mejora el objetivo |
| `cifras` | totales de un conjunto: personas, pagos, entrenos, km, score |
| `consultar` | una fila por persona para comparar o razonar (tope de 60); con filtro de evento, el tiempo objetivo que capturó al inscribirse, su resultado y la diferencia |
| `grafica` | por semana, ranking o dispersión del score, como imagen |
| `catalogo` | qué existe, por tema y solo para el modelo: `eventos` de cualquier año (fecha, inscritos, con resultado), `grupos`, `convenios` y `calendario` (entrenos que pusieron los coaches, por título o tipo), con texto para acotar |
| `preguntar` | una pregunta al admin con botones |
| `anotar_faltante` | deja en `state/audit.log` lo que pidieron y ninguna herramienta pudo traer (evento `tool`, con el pedido) |

Si el nombre de un evento o de un grupo coincide con más de los que se pidieron (otro año, otra distancia, «42k
MTY» con sus varios ritmos), la consulta no sale: el admin recibe la pregunta con un botón por opción y «Ambos» o
«Todos» (`Toolbox._filtered`, `which_event` y `which_group`, `duma/tools.py`); con ocho o más, Duma pregunta en
palabras. Cuándo un «este año» o el contexto bastan para no preguntar lo decide el modelo, con las reglas de
`prompts/system.md`.

**Filtros** de `buscar_atletas`, `cifras`, `consultar` y `grafica`, combinables: evento, pago en un periodo, grupo
(uno o varios), entrenos hechos, membresía (vigente, vencida, sin membresía, vence en un periodo), comprobante
(pendiente, aprobado, rechazado; beneficio o pago), perfil (nivel, sede, género, con o sin reloj) y faltas (entrenos
prescritos sin hacer).

No tiene: correo, teléfono ni contacto de nadie; la descripción que el coach escribe en un entreno (el título sí,
por `catalogo`). `errores_garmin` no cubre a quien no tiene reloj vinculado, y `avisos_enviados` no trae el resumen
semanal ni la cuenta regresiva de eventos, que son automáticos.

## Qué puede escribir

Seis cosas: decidir un comprobante, decidir una solicitud de ingreso, mandar un aviso, renovar una membresía,
pausar o reactivar a un atleta y registrar un tiempo de carrera. En todas el modelo solo propone: las rutas que
escriben están en `WRITES` (`duma/api_client.py`) y solo las llama el manejador del botón (`duma/main.py`). La
propuesta es de un solo uso, caduca (`BOT_CONFIRM_TTL_MIN`) y solo la decide quien la pidió. El API la registra como
el usuario de consola ligado al Telegram de quien pulsó; hoy solo hay uno (`ASSISTANT_ADMINS` en
`muungano-api/security.py`) y a cualquier otro le contesta 403.

**Espera antes de ejecutar.** Al pulsar un botón que escribe en el API, la tarjeta cambia a «Por seguridad, esta
acción se ejecutará en 10 segundos» con un botón «Cancelar» (`BOT_ACTION_DELAY_S`; `Bot._on_button` y `_on_stop`,
`duma/main.py`). Cancelar no hace nada y devuelve la tarjeta con sus botones, lista para decidirse otra vez. No hay
deshacer después: lo ejecutado se corrige en la consola. Si el bot se reinicia durante la espera, la acción no corre.

### Comprobantes

- `revisar_comprobante` manda la foto, una tarjeta (plan pedido, monto leído contra el esperado, cobertura) y los
  botones: un plan por botón, un motivo de rechazo por botón y «Dejar pendiente».
- `rechazar_comprobante` propone un rechazo con un motivo propio, con «Rechazar» y «Cancelar».
- El clic llama a `POST /assistant/receipts/{id}/decide` (`Bot._decide_receipt`).
- Aprobar hace lo mismo que la consola: activa la membresía, manda correo al atleta y publica sus entrenos en Garmin.

### Solicitudes de ingreso

- `revisar_solicitud` manda una tarjeta (nombre, ciudad, género, edad aproximada, fecha, comentario y si llenó el
  cuestionario) con los tres botones de la consola, «Aceptar», «Lista de espera» y «Rechazar», y «Dejar pendiente».
  Sin argumentos toma la pendiente más antigua que ya tiene cuestionario.
- Sin cuestionario no hay botón de aceptar. Correo y teléfono no salen.
- El clic llama a `POST /assistant/applications/{id}/decide` (`Bot._decide_application`), que hace lo mismo que la
  consola: cambia el estado, le manda correo a la persona y, al rechazar, le cierra el acceso.
- Grupo y nivel no se asignan aquí: eso sigue en la consola.
- **Aviso automático.** Cuando alguien termina el cuestionario, el API le avisa a Duma por un puerto local
  (`duma/hooks.py`, `BOT_HOOK_PORT`, solo 127.0.0.1 y con el token compartido) y Duma publica la tarjeta con sus
  botones en el tema «Solicitudes» (`Bot.on_hook`, `duma/main.py`), que abre la primera vez y recuerda en
  `state/solicitudes.topic`. Esas tarjetas no las pidió nadie: las decide cualquier admin del grupo que el API
  reconozca y duran 60 días. Si Duma está caído cuando llega el aviso, esa tarjeta no se publica; la solicitud
  sigue saliendo en `solicitudes`.

### Renovar, pausar y tiempos de carrera

Las tres muestran una propuesta con su botón y «Cancelar»; la llamada al API queda guardada tal como se mostró y el
clic la manda (`Bot._write_member`, `duma/main.py`).

- `renovar_membresia`: el «Renovar membresía» de la consola, para un pago recibido fuera de la app. Muestra el plan
  (1, 3 o 6 meses), el precio que pone el servidor, el monto recibido si lo dijeron, la referencia y hasta cuándo
  quedaría cubierto. No procede si el atleta tiene un comprobante en revisión.
- `pausar_atleta`: pausa o reactiva. No archiva ni trae de vuelta a un archivado.
- `tiempo_carrera`: registra el tiempo final (H:MM:SS) en un evento al que el atleta está inscrito; si el nombre
  coincide con varios o con ninguno, no propone y el modelo pregunta.

### Avisos por correo y push

- `proponer_aviso` arma la tarjeta: texto completo, canal (correo, push o ambos), a cuántos atletas llega, cuántos
  tienen push y cuántos correo, y la lista (nombres en la tarjeta hasta 10; si son más, un CSV). Botones «Enviar» y
  «Cancelar».
- **La audiencia siempre son atletas activos** y hay que decirla: «todos» de forma expresa, los filtros de
  `buscar_atletas` (grupo, evento, membresía, perfil, faltas) o personas concretas; filtros y personas se suman. Si
  un nombre de grupo o evento no coincide con ninguno, no se propone nada.
- **La lista se congela al proponer**: los ids se guardan con la propuesta y el envío usa esos. Quien dejó de estar
  activo entre la propuesta y el clic se queda fuera, y el resultado lo dice.
- El clic llama a `POST /assistant/messages` (`Bot._send_announcement`). El API contesta en cuanto acepta el envío
  y lo hace después; el mensaje se reescribe con «envió la solicitud al servidor» y a cuántos atletas. Cuántos
  aceptó cada canal se ve con `avisos_enviados`.
- Es el mismo envío que la pantalla de Avisos de la consola: canal, categoría, asunto y mensaje, el mismo texto para
  todos. No hay envío programado.

También guarda **preferencias permanentes** del equipo (`duma/preferences.py`), igual con botón de confirmar.

## Cómo salen las respuestas

- Texto plano: Telegram no interpreta Markdown aquí.
- **Una respuesta por pregunta.** Lo que las herramientas y el modelo producen en un turno se junta y sale al final
  (`Bot._flush`, `duma/main.py`): las imágenes como un álbum, los archivos, y el texto al pie del último si cabe o
  como mensaje aparte; las tarjetas con botones van después, cada una en su mensaje. Solo el último mensaje del
  turno notifica; los demás salen en silencio, igual que «Entendí: …» de una nota de voz y «Compactando sesión…».
- Tablas (entrenos, vueltas, plan, pagos, comprobantes, solicitudes, errores de Garmin, avisos enviados): imagen hasta 40 filas, CSV si son más, o un álbum de
  imágenes repartidas parejo si el admin pide imagen (`Toolbox._deliver` y `_tables`, `duma/tools.py`).
- Gráficas como imagen (`duma/charts.py`).

## Privacidad

- El modelo nunca recibe nombres: ve códigos `ATLETA_NN` (`duma/pseudonyms.py`) y el bot pone el nombre al escribir
  en el chat. Los nombres de grupos y eventos sí los ve.
- Lo que escribió una persona (meta del cuestionario, comentario de una solicitud, motivo de rechazo, referencia
  leída de un comprobante, asunto de un aviso enviado) sale al chat o al CSV, no al modelo. El texto de un aviso nuevo sí pasa por el modelo: se lo
  dicta el administrador.
- La foto de un comprobante va del API al chat sin pasar por el modelo.

## Sesiones y costo

- Una sesión por tema, guardada en Postgres (esquema `duma`). Se compacta sola al pasar `BOT_SESSION_MAX_TOKENS` y
  se cierra tras `BOT_SESSION_IDLE_HOURS` sin mensajes (0 = nunca).
- Si cambian las instrucciones o las herramientas, la sesión descarta sus bloques de razonamiento antes del
  siguiente mensaje (`Agent._align`, `duma/agent.py`).
- Tope de gasto diario `BOT_DAILY_BUDGET_USD`; `/usage` muestra el costo del tema.
- `state/audit.log` registra cada herramienta llamada y cada clic, con quién lo hizo.

## Pruebas

`.venv/bin/python -m pytest -q` (308 pruebas). El CI las corre y, si pasan, llama a `/deploy/bot`. No hay lint en CI.
