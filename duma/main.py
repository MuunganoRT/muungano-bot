"""The polling loop: Telegram in, Duma's answer out."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from duma.agent import Agent, AnthropicLLM, Session
from duma.api_client import ApiError, MuunganoApi
from duma.audit import Audit
from duma.auth import decide
from duma import confirmations, hooks, media, voice
from duma.budget import Budget
from duma.config import ConfigError, Settings, load
from duma.database import Database, DatabaseError, PostgresDatabase, SqliteDatabase
from duma.confirmations import Confirmations
from duma.preferences import Preferences
from duma.render import _day_year
from duma.sessions import SessionStore
from duma.telegram_api import MAX_CAPTION, Telegram, TelegramError
from duma.tools import OutFile, Toolbox, application_card, parse_choice
from duma.usage import PRICES, Usage, cost_usd

log = logging.getLogger("duma")

ROOT = Path(__file__).resolve().parent.parent
PROMPT = ROOT / "prompts" / "system.md"

HELP = (
    "Soy Duma, el cheetah de datos de Muungano RT. Te traigo el resumen de un atleta, busco personas por nombre, "
    "listo a quienes cumplen un filtro (evento, pago, grupo, entrenos) y te doy cifras del equipo.\n"
    "En General, toca /ruun: abro un tema y ahí me escribes normal, sin comando. También puedes poner la pregunta "
    "junto al comando.\n"
    "/ruun <pregunta>: pregunta nueva · /clear: sesión limpia · /usage: costo y contexto · "
    "/prefs: reglas permanentes · /forget <n>: quitar una · /help: esto"
)
RUN = "ruun"
# What a hurried thumb types instead.
RUN_ALIASES = (RUN, "run", "runn", "duma")
READY = "Sí, dime."
NEW_TOPIC = "Nueva pregunta"
# What Telegram offers when an admin types `/`. Registered at startup, for the admin group only.
COMMANDS = [
    (RUN, "Pregunta nueva: abre un tema y te leo ahí"),
    ("usage", "Costo y contexto de este tema"),
    ("clear", "Sesión limpia en este tema"),
    ("prefs", "Reglas permanentes guardadas"),
    ("forget", "Quitar una regla: /forget 2"),
    ("help", "Qué sé hacer"),
]
IN_LOBBY_NEW = "Cada tema tiene su propia sesión. Para reiniciar una, escribe /clear dentro del tema."
IN_LOBBY_USAGE = "Cada tema lleva su propia cuenta. Escribe /usage dentro del tema que quieras revisar."
ASK_WITH_RUN = (
    "En General solo contesto con /ruun, para no abrir un tema por cada mensaje. "
    "Por ejemplo: /ruun ¿cuántos inactivos hay? · /help te dice qué más hay."
)
OVER_BUDGET = "Ya se gastó el tope de hoy (${limit:.2f} USD) y no puedo contestar más preguntas. Mañana seguimos."
BUDGET_REACHED = "Con esta respuesta se alcanzó el tope de hoy (${limit:.2f} USD). Mañana seguimos."
NO_PREFS = "No hay reglas permanentes guardadas. Pídeme una con algo como «siempre que te pida X, mándalo así»."
FORGET_NEEDS_NUMBER = "Dime cuál con su número, por ejemplo /forget 2. La lista sale con /prefs."
NOT_YOURS = "Solo quien lo pidió puede decidir."
WAITING = "Por seguridad, esta acción se ejecutará en {seconds} segundos."
STOPPED = "Cancelado: no se hizo nada."
TOO_LATE = "Ya se ejecutó o ya no está vigente."
NOTICE_TOPIC = "Solicitudes"
# A card Duma posts on its own waits for as long as the application may.
NOTICE_TTL_S = 60 * 24 * 3600
# Who the API's log says asked when it was a hook and no admin.
HOOK_CALLER = 0
# The kinds of pending action that end in `Bot._decide_receipt`.
RECEIPT_KINDS = ("receipt", "receipt_reject")
NO_LONGER_VALID = "Esto ya se decidió o ya no está vigente."
EXPIRED = "Caducó sin decidirse. Pídemelo otra vez."
# A chat among admins in General must not get the reminder on every line.
HINT_EVERY_S = 600
COMPACTING = "Compactando sesión…"
FRESH_SESSION = "No pude compactarla, así que arrancamos sesión limpia: la anterior se alargó demasiado."
CLOSING_IDLE = "Cierro este tema por inactividad y olvido su conversación. Para seguir, pregúntame de nuevo con /ruun en General."
SWEEP_EVERY_S = 600
DOWNLOAD_FAILED = "No pude descargar el archivo. Mándamelo otra vez."
UNKNOWN_COMMAND = "No conozco ese comando. /help te dice qué sé hacer."
# In a supergroup with topics on, the "General" topic is thread 1. A send with no thread id lands there.
GENERAL_TOPIC = 1
# General is the lobby: each `/ruun` written there opens its own topic, and the topic is the session. Opening one
# needs the bot to be an admin with `can_manage_topics`; without it (or in a group without topics) Duma answers in
# General and tries again after this many seconds.
TOPIC_RETRY_S = 600
TITLE_MAX = 40


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # `httpx` logs every request URL at INFO, and a Telegram URL contains the bot token.
    for noisy in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _command(text: str) -> Optional[str]:
    """`/help@DumaBot args` -> `help`; None when the text is not a command."""
    if not text.startswith("/"):
        return None
    return text[1:].split()[0].split("@")[0].lower() if text[1:].strip() else ""


def _keyboard(buttons: Optional[list[tuple[str, str]]]) -> Optional[dict[str, Any]]:
    if not buttons:
        return None
    cells = [{"text": label, "callback_data": data} for label, data in buttons]
    # Guardar/Cancelar sit side by side; the options of a question go one under another, so their text fits.
    if any(parse_choice(data) for _, data in buttons):
        return {"inline_keyboard": [[cell] for cell in cells]}
    if any(confirmations.parse_application(data) for _, data in buttons):
        return {"inline_keyboard": [cells[:-1], cells[-1:]]}
    if any(confirmations.parse_receipt(data) for _, data in buttons):
        # The plans share a row; each way out of approving gets its own.
        plans = [cell for cell in cells if cell["callback_data"].split(":")[1][:1] == "a"]
        return {"inline_keyboard": [plans, *[[cell] for cell in cells if cell not in plans]]}
    return {"inline_keyboard": [cells]}


def _arguments(text: str) -> str:
    """What follows the command: `/ruun cuántos hay` -> `cuántos hay`."""
    parts = text.split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _usd(model: str, usage: Usage) -> str:
    cost = cost_usd(model, usage)
    return "sin precio para este modelo" if cost is None else f"${cost:.4f} USD"


def _k(tokens: int) -> str:
    """2253 -> `2.3k`, 100000 -> `100k`, 246 -> `246`."""
    for size, suffix in ((1_000_000, "M"), (1_000, "k")):
        if tokens >= size:
            return f"{tokens / size:.1f}".removesuffix(".0") + suffix
    return str(tokens)


def usage_report(model: str, limit: int, context: int, spent: Usage, fixed: Optional[tuple[int, int]]) -> str:
    lines = [
        f"{_usd(model, spent)} en {spent.calls} llamada(s) al modelo",
        "Tokens:",
        f"· Entrada: {_k(spent.input)}",
        f"· Caché escrita: {_k(spent.cache_write)}",
        f"· Caché leída: {_k(spent.cache_read)}",
        f"· Salida: {_k(spent.output)}",
    ]
    if context:
        lines.append(f"Contexto ahora: {_k(context)} de {_k(limit)} tokens ({round(100 * context / limit)}%)")
        if fixed:
            instructions, tools = fixed
            lines.append(f"· Instrucciones: {_k(instructions)}")
            lines.append(f"· Herramientas: {_k(tools)}")
            lines.append(f"· Conversación: {_k(max(context - instructions - tools, 0))}")
    else:
        lines.append("Contexto: vacío, todavía no hay conversación.")
    return "\n".join(lines)


def topic_title(text: str, limit: int = TITLE_MAX) -> str:
    """The first words of a request, on one line, as the name of its topic."""
    line = " ".join(text.split())
    if len(line) <= limit:
        return line or "Consulta"
    cut = line[:limit].rsplit(" ", 1)[0] or line[:limit]
    return cut.rstrip(" ,.;:") + "…"


def topic_link(chat_id: int, thread_id: int) -> str:
    # A supergroup id looks like -100<internal id>; t.me/c/ takes the internal one.
    raw = str(chat_id)
    internal = raw[4:] if raw.startswith("-100") else str(abs(chat_id))
    return f"https://t.me/c/{internal}/{thread_id}"


@dataclass
class Countdown:
    """A decision that was clicked and has not run yet."""

    task: asyncio.Task
    # The call that will write to the API; never started if it is called off.
    run: Any
    user_id: int
    summary: str
    # The card's own buttons, to put them back.
    keyboard: Optional[dict[str, Any]]


class Bot:
    def __init__(
        self,
        settings: Settings,
        telegram: Telegram,
        agent: Agent,
        audit: Audit,
        clock=time.monotonic,
        wall_clock=time.time,
        api: Optional[MuunganoApi] = None,
        budget: Optional[Budget] = None,
        confirmations: Optional[Confirmations] = None,
        preferences: Optional[Preferences] = None,
        store: Optional[SessionStore] = None,
        transcriber: Optional[voice.Transcriber] = None,
    ):
        self._store = store
        self._transcriber = transcriber
        self._next_sweep = 0.0
        self._api = api
        self._budget = budget
        self._confirmations = confirmations
        self._countdowns: dict[str, Countdown] = {}
        self._notices = settings.state_dir / "solicitudes.topic"
        self._preferences = preferences
        self._s = settings
        self._tg = telegram
        self._agent = agent
        self._audit = audit
        self._clock = clock
        self._wall_clock = wall_clock
        self._topics_retry_at = 0.0
        self._hinted_at: dict[int, float] = {}
        # Topics opened by a bare `/ruun`: they take their name from the first thing asked in them. On disk, so a
        # deploy between the tap and the question does not leave one called "Nueva pregunta" for good.
        self._untitled_file = settings.state_dir / "untitled.topics"
        try:
            self._untitled: set[int] = {int(line) for line in self._untitled_file.read_text().split()}
        except (OSError, ValueError):
            self._untitled = set()
        # One session per (admin, topic); topic 0 is General when topics cannot be opened.
        self._sessions: dict[tuple[int, int], Session] = {}

    def _session(self, user_id: int, thread_id: int) -> Session:
        return self._sessions.setdefault((user_id, thread_id), Session())

    async def _open(self, user_id: int, thread_id: int) -> Session:
        """The session of that admin in that topic: from memory, else as it was saved, else a new one."""
        key = (user_id, thread_id)
        if key not in self._sessions and self._store is not None:
            try:
                saved = await self._store.load(user_id, thread_id)
            except Exception as exc:  # the database is down: answer with a fresh session rather than not at all
                log.warning("could not load a session: %s", type(exc).__name__)
                saved = None
            # Two messages can get here together; whoever stored first wins, so both share one lock.
            self._sessions.setdefault(key, saved or Session())
        return self._session(user_id, thread_id)

    async def _save(self, user_id: int, thread_id: int) -> None:
        if self._store is None:
            return
        try:
            await self._store.save(user_id, thread_id, self._sessions[(user_id, thread_id)])
        except Exception as exc:  # a database that is down must not cost the admin their answer
            log.warning("could not save a session: %s", type(exc).__name__)

    async def sweep(self) -> None:
        """Close the topics nobody has written in for a while and delete their sessions."""
        idle_s = self._s.session_idle_hours * 3600
        if self._store is None or idle_s <= 0 or self._clock() < self._next_sweep:
            return
        self._next_sweep = self._clock() + SWEEP_EVERY_S
        if self._confirmations is not None:
            await self._confirmations.purge()
        for thread_id in await self._store.idle_topics(idle_s):
            if any(s.lock.locked() for (_, t), s in self._sessions.items() if t == thread_id):
                continue  # someone is being answered there right now
            await self._store.delete_topic(thread_id)
            for key in [k for k in self._sessions if k[1] == thread_id]:
                del self._sessions[key]
            if not thread_id:
                continue  # General has no topic to close
            try:
                await self._tg.send_message(self._s.admin_chat_id, CLOSING_IDLE, thread_id)
                await self._tg.close_forum_topic(self._s.admin_chat_id, thread_id)
            except TelegramError as exc:  # deleted by hand, or already closed
                log.info("could not close idle topic %s: %s", thread_id, exc)
            else:
                log.info("closed idle topic %s", thread_id)

    async def check(self) -> None:
        """Say at startup what Duma can see, so a silent bot is not a mystery.

        Everything it logs here is an id, a name or a permission: nothing secret.
        """
        admin = self._s.admin_chat_id
        try:
            me = await self._tg.get_me()
        except TelegramError as exc:
            log.error("cannot talk to Telegram as the bot (%s): check TELEGRAM_BOT_TOKEN", exc)
            return
        log.info("connected as @%s", me.get("username"))
        log.info("admin group %s, %d allowed user(s)", admin, len(self._s.allowed_user_ids))
        try:
            chat = await self._tg.get_chat(admin)
            member = await self._tg.get_chat_member(admin, me["id"])
        except TelegramError as exc:
            log.error("cannot see the admin group (%s): TELEGRAM_ADMIN_CHAT_ID is wrong, or Duma is not in the group", exc)
            return
        status = member.get("status")
        log.info("group %r: %s, topics %s; Duma is %s", chat.get("title"), chat.get("type"), "on" if chat.get("is_forum") else "off", status)
        if chat.get("type") != "supergroup":
            log.warning("the admin group is not a supergroup: its id changes if Telegram converts it")
        if status not in ("member", "administrator"):
            log.error("Duma is not a member of the admin group (status: %s)", status)
        elif chat.get("is_forum") and not member.get("can_manage_topics"):
            log.warning("Duma cannot manage topics: it will answer in General. Make it admin with only that permission")
        elif not chat.get("is_forum"):
            log.info("topics are off in this group: Duma will answer in General")
        try:
            await self._tg.set_my_commands(COMMANDS, admin)
        except TelegramError as exc:
            log.warning("could not register the command list (%s): typing / will not suggest commands", exc)
        self._check_budget()
        if self._transcriber is not None:
            log.info("voice notes: Whisper %s, on this machine", self._s.voice_model)
            self._preparing = asyncio.create_task(self._prepare_voice())
        else:
            log.info("voice notes are off: the speech model is not installed (pip install -e '.[voice]')")
        if self._store is not None:
            hours = self._s.session_idle_hours
            log.info("sessions are kept; %s", f"a topic idle for {hours} h is closed" if hours > 0 else "idle topics are never closed")
        await self._check_api()

    async def _prepare_voice(self) -> None:
        if await self._transcriber.prepare():
            log.info("the speech model is ready")

    def _check_budget(self) -> None:
        if self._budget is None:
            return
        if self._budget.limit_usd <= 0:
            log.warning("no daily budget: BOT_DAILY_BUDGET_USD is 0")
        elif self._s.model not in PRICES:
            log.warning("no price for model %s: its spend cannot be counted, so the daily budget will not stop it", self._s.model)
        else:
            log.info("daily budget: $%.2f, spent today $%.4f", self._budget.limit_usd, self._budget.spent())

    async def _check_api(self) -> None:
        """One harmless lookup, so a wrong URL or token shows up now and not as a vague answer later."""
        if self._api is None:
            return
        try:
            await self._api.get("/assistant/athletes", telegram_user_id=min(self._s.allowed_user_ids), params={"q": "zz"})
        except ApiError as exc:
            if exc.status == 401:
                log.error(
                    "the API at %s rejected the token: ASSISTANT_TOKEN in the API's .env must equal BOT_API_TOKEN, "
                    "and the API must be restarted after changing it. Is another server listening on that port?",
                    self._s.api_url,
                )
            elif exc.status == 404:
                log.error("the API at %s has no /assistant routes: wrong server on that port, or an old build", self._s.api_url)
            else:
                log.error("cannot use the API at %s: %s", self._s.api_url, exc.message)
            return
        log.info("API ok at %s", self._s.api_url)

    async def handle(self, update: dict[str, Any]) -> None:
        # What arrived, not what it says: if nothing shows here, Telegram is not delivering to this bot.
        log.info("update received: %s", ", ".join(k for k in update if k != "update_id"))
        decision = decide(update, self._s)
        if decision.action == "leave":
            log.warning("added to a chat that is not the admin group (%s): leaving", decision.chat_id)
            await self._tg.leave_chat(decision.chat_id)
            return
        if decision.action == "migrated":
            log.warning(
                "the admin group became a supergroup: set TELEGRAM_ADMIN_CHAT_ID to %s and restart", decision.chat_id
            )
            return
        if decision.action == "ignore":
            self._log_ignored(update, decision.reason)
            return

        message = update.get("message")
        if not message:
            if update.get("callback_query"):
                await self._on_button(update["callback_query"])
            return
        age = self._wall_clock() - message.get("date", self._wall_clock())
        if age > self._s.stale_after_s:
            # Written while Duma was down: answering it now, and opening a topic for it, would be noise.
            log.info("skipped a message from %d s ago (older than %d s): chat=%s", age, self._s.stale_after_s, message["chat"]["id"])
            return
        await self._on_message(message)

    async def _on_button(self, query: dict[str, Any]) -> None:
        """A click on a button of a proposal. This, and nothing the model says, is what runs an action."""
        choice = parse_choice(query.get("data", ""))
        if choice is not None:
            await self._on_choice(query, *choice)
            return
        stopped = confirmations.parse_stop(query.get("data", ""))
        if stopped is not None:
            await self._on_stop(query, stopped)
            return
        on_application = confirmations.parse_application(query.get("data", ""))
        on_card = confirmations.parse_receipt(query.get("data", "")) or on_application
        parsed = on_card or confirmations.parse(query.get("data", ""))
        if parsed is None or self._confirmations is None:
            await self._tg.answer_callback_query(query["id"])
            return
        verb, action_id = parsed
        user_id = query["from"]["id"]
        status, pending = await self._confirmations.claim(action_id, user_id)
        if status == "not_yours":
            await self._tg.answer_callback_query(query["id"], NOT_YOURS)
            return

        who = (query["from"].get("first_name") or "").strip() or "un administrador"
        shown = query.get("message") or {}
        run = None
        if status == "ok" and verb in (confirmations.CANCEL, confirmations.CLOSE):
            outcome = "Sigue pendiente." if on_card else "Cancelado."
        elif status == "ok" and (pending.kind == "application") != bool(on_application):
            # A button of one kind of card pointing at a proposal of another: nothing of ours sends that.
            outcome = NO_LONGER_VALID
        elif status == "ok" and pending.kind == "application":
            run = self._decide_application(pending.payload["athlete"], verb, user_id, who)
        elif status == "ok" and pending.kind in RECEIPT_KINDS:
            choice = confirmations.decision(verb) if on_card else pending.payload["decision"]
            run = self._decide_receipt(pending.payload["receipt"], choice, user_id, who)
        elif status == "ok" and pending.kind == "announcement":
            run = self._send_announcement(pending.payload, user_id, who)
        elif status == "ok" and pending.kind == "member_write":
            run = self._write_member(pending.payload, user_id, who)
        elif status == "ok":
            outcome = self._execute(pending.kind, pending.payload, user_id)
        else:
            outcome = EXPIRED if status == "expired" else NO_LONGER_VALID

        def audit(result: str) -> None:
            self._audit.log(
                "confirmation", user=user_id, action=action_id, kind=pending.kind if pending else None, verb=verb,
                status=result, receipt=pending.payload.get("receipt") if pending else None,
            )

        async def rewrite(text: str, keyboard: Optional[dict[str, Any]] = None) -> None:
            if not shown.get("message_id"):
                return
            try:
                await self._tg.edit_message_text(shown["chat"]["id"], shown["message_id"], text, keyboard)
            except TelegramError as exc:
                log.info("could not rewrite a decided proposal: %s", exc)

        delay = self._s.action_delay_s
        if run is not None and delay > 0 and shown.get("message_id"):
            # What writes to the API waits, with a way out: a slip of the finger costs nothing.
            pressed = next(
                (
                    cell.get("text")
                    for row in (shown.get("reply_markup") or {}).get("inline_keyboard") or []
                    for cell in row
                    if cell.get("callback_data") == query.get("data")
                ),
                None,
            )

            async def later() -> None:
                await asyncio.sleep(delay)
                if self._countdowns.pop(action_id, None) is None:
                    return
                result = await run
                audit("ok")
                await rewrite(f"{pending.summary}\n\n{result}")

            self._countdowns[action_id] = Countdown(asyncio.create_task(later()), run, user_id, pending.summary, shown.get("reply_markup"))
            await self._tg.answer_callback_query(query["id"], WAITING.format(seconds=delay))
            chosen = f"{who}: «{pressed}». " if pressed else ""
            await rewrite(
                f"{pending.summary}\n\n{chosen}{WAITING.format(seconds=delay)}",
                _keyboard([("Cancelar", f"{confirmations.STOP}:{action_id}")]),
            )
            return

        if run is not None:
            outcome = await run
        audit(status)
        await self._tg.answer_callback_query(query["id"], outcome)
        if status != "gone":  # else it was already rewritten by the click that decided it
            await rewrite(f"{pending.summary}\n\n{outcome}")

    async def _on_stop(self, query: dict[str, Any], action_id: str) -> None:
        """Cancelar on a decision that is waiting to run: nothing happens and the card is as it was."""
        waiting = self._countdowns.get(action_id)
        if waiting is None or self._confirmations is None:
            await self._tg.answer_callback_query(query["id"], TOO_LATE)
            return
        if waiting.user_id != query["from"]["id"]:
            await self._tg.answer_callback_query(query["id"], NOT_YOURS)
            return
        del self._countdowns[action_id]
        waiting.task.cancel()
        waiting.run.close()
        await self._confirmations.release(action_id)
        self._audit.log("confirmation", user=waiting.user_id, action=action_id, verb=confirmations.STOP, status="stopped")
        await self._tg.answer_callback_query(query["id"], STOPPED)
        shown = query.get("message") or {}
        if not shown.get("message_id"):
            return
        try:
            await self._tg.edit_message_text(shown["chat"]["id"], shown["message_id"], waiting.summary, waiting.keyboard)
        except TelegramError as exc:
            log.info("could not restore a proposal: %s", exc)

    async def _on_choice(self, query: dict[str, Any], asked: int, index: int) -> None:
        """A click on an option of a question: the same as the admin typing that option in that topic."""
        user_id = query["from"]["id"]
        if user_id != asked:
            await self._tg.answer_callback_query(query["id"], NOT_YOURS)
            return
        shown = query.get("message") or {}
        rows = (shown.get("reply_markup") or {}).get("inline_keyboard") or []
        label = next(
            (cell.get("text") for row in rows for cell in row if cell.get("callback_data") == query.get("data")), None
        )
        await self._tg.answer_callback_query(query["id"])
        if not label or not shown.get("message_id"):
            return  # the buttons are already gone: a second click on a question that was answered
        chat_id = shown["chat"]["id"]
        try:
            # Rewritten without a keyboard: the choice stays in the chat and cannot be clicked twice.
            await self._tg.edit_message_text(chat_id, shown["message_id"], f"{shown.get('text', '')}\n\n→ {label}".strip())
        except TelegramError as exc:
            log.info("could not rewrite an answered question: %s", exc)
        thread_id = shown.get("message_thread_id") if shown.get("is_topic_message") else None
        if thread_id == GENERAL_TOPIC:
            thread_id = None
        if await self._over_budget(chat_id, thread_id):
            return
        await self._ask(chat_id, user_id, thread_id, label)

    async def _decide_receipt(self, receipt_id: int, choice: Optional[dict[str, Any]], user_id: int, who: str) -> str:
        """Approve or reject a receipt in the API, as the admin who pressed the button."""
        if self._api is None or choice is None:
            return NO_LONGER_VALID
        try:
            done = await self._api.write(
                f"/assistant/receipts/{receipt_id}/decide", telegram_user_id=user_id, json=choice
            )
        except ApiError as exc:
            log.warning("deciding receipt %s failed: API status %s: %s", receipt_id, exc.status, exc.message)
            return f"No se pudo: {exc.message}"
        if choice["accion"] == "rechazar":
            return f"Rechazado por {who}. Motivo que recibe el atleta: «{choice['motivo']}»"
        months = choice["meses"]
        until = (done.get("data") or {}).get("cubierto_hasta")
        covered = f", cubierto hasta {_day_year(until)}" if until else ""
        return f"Aprobado por {who}: {months} {'mes' if months == 1 else 'meses'}{covered}."

    async def on_hook(self, event: str, data: dict[str, Any]) -> None:
        """Something the API says just happened. Today: a signup whose questionnaire was just answered."""
        if event != "application" or self._api is None or self._confirmations is None:
            return
        athlete_id = data.get("athlete_id")
        if isinstance(athlete_id, bool) or not isinstance(athlete_id, int):
            return
        found = await self._api.get(f"/assistant/applications/{athlete_id}", telegram_user_id=HOOK_CALLER)
        application = found["application"]
        card = application_card(application)
        action_id = await self._confirmations.propose(
            confirmations.ANYONE, "application", {"athlete": athlete_id}, card, ttl_s=NOTICE_TTL_S
        )
        keyboard = _keyboard(confirmations.application_buttons(action_id, application["questionnaire"]))
        chat_id = self._s.admin_chat_id
        self._audit.log("hook", what=event, athlete=athlete_id)
        try:
            await self._tg.send_message(chat_id, card, await self._notice_topic(chat_id), keyboard)
        except TelegramError:
            # The topic was deleted by hand: open another and remember that one.
            self._notices.unlink(missing_ok=True)
            await self._tg.send_message(chat_id, card, await self._notice_topic(chat_id), keyboard)

    async def _notice_topic(self, chat_id: int) -> Optional[int]:
        """The topic where applications are posted: opened once and remembered across restarts."""
        try:
            return int(self._notices.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            pass
        try:
            thread_id = await self._tg.create_forum_topic(chat_id, NOTICE_TOPIC)
        except TelegramError as exc:
            log.warning("could not open the %s topic (%s); posting in General", NOTICE_TOPIC, exc)
            return None
        self._notices.parent.mkdir(parents=True, exist_ok=True)
        self._notices.write_text(str(thread_id), encoding="utf-8")
        return thread_id

    async def _decide_application(self, athlete_id: int, code: str, user_id: int, who: str) -> str:
        """Accept, wait-list or reject a signup in the API, as the admin who pressed the button."""
        if self._api is None:
            return NO_LONGER_VALID
        action, done = confirmations.APPLICATION_ACTIONS[code]
        try:
            await self._api.write(
                f"/assistant/applications/{athlete_id}/decide", telegram_user_id=user_id, json={"action": action}
            )
        except ApiError as exc:
            log.warning("deciding application %s failed: API status %s: %s", athlete_id, exc.status, exc.message)
            return f"No se pudo: {exc.message}"
        return f"{done} por {who}. Se le avisa por correo."

    async def _write_member(self, payload: dict[str, Any], user_id: int, who: str) -> str:
        """Renew, pause or record a race time: the call fixed when it was proposed, as whoever pressed."""
        if self._api is None:
            return NO_LONGER_VALID
        try:
            done = await self._api.write(payload["path"], telegram_user_id=user_id, json=payload["json"])
        except ApiError as exc:
            log.warning("%s failed: API status %s: %s", payload["path"], exc.status, exc.message)
            return f"No se pudo: {exc.message}"
        until = done.get("covered_until")
        covered = f" Cubierto hasta {_day_year(until)}." if until else ""
        return f"{payload['done']} por {who}.{covered}"

    async def _send_announcement(self, payload: dict[str, Any], user_id: int, who: str) -> str:
        """Hand an announcement to the API, for the list fixed when it was proposed. The API sends it afterwards."""
        if self._api is None:
            return NO_LONGER_VALID
        try:
            done = await self._api.write("/assistant/messages", telegram_user_id=user_id, json=payload)
        except ApiError as exc:
            log.warning("sending an announcement failed: API status %s: %s", exc.status, exc.message)
            return f"No se envió: {exc.message}"
        left = f" {done['dropped']} ya no estaban activos y se quedaron fuera." if done.get("dropped") else ""
        return f"{who} envió la solicitud al servidor: {done['recipients']} atleta(s).{left}"

    def _execute(self, kind: str, payload: dict[str, Any], user_id: int) -> str:
        """Run a confirmed action and say what happened. Every kind of action with an effect is listed here."""
        if self._preferences is None:
            return NO_LONGER_VALID
        try:
            if kind == "preference_add":
                self._preferences.add(user_id, payload["rule"], tuple(payload.get("replace") or ()))
                return "Guardada. Aplica desde el siguiente mensaje."
            if kind == "preference_remove":
                return "Olvidada." if self._preferences.remove(payload["line"]) else "Esa regla ya no estaba."
        except ValueError as exc:
            return str(exc)
        log.warning("a confirmed action of an unknown kind was not run: %s", kind)
        return NO_LONGER_VALID

    def _remember_untitled(self) -> None:
        try:
            self._untitled_file.parent.mkdir(parents=True, exist_ok=True)
            self._untitled_file.write_text("\n".join(str(t) for t in sorted(self._untitled)))
        except OSError as exc:
            log.warning("could not save the untitled topics: %s", type(exc).__name__)

    def _lobby_open(self) -> bool:
        """Whether General should open a topic per request right now."""
        return self._clock() >= self._topics_retry_at

    async def _open_topic(self, chat_id: int, message: dict[str, Any], text: str) -> Optional[int]:
        title = topic_title(text) if text else NEW_TOPIC
        try:
            thread_id = await self._tg.create_forum_topic(chat_id, title)
        except TelegramError as exc:
            self._topics_retry_at = self._clock() + TOPIC_RETRY_S
            log.warning("could not open a topic (%s); answering in General for %d minutes", exc, TOPIC_RETRY_S // 60)
            return None
        # The id is what a topic can be deleted by later; the title is request text, so it stays out of the log.
        log.info("opened topic %s (%s)", thread_id, topic_link(chat_id, thread_id))
        if not text:
            self._untitled.add(thread_id)
            self._remember_untitled()
            await self._tg.send_message(chat_id, f"Te leo aquí: {topic_link(chat_id, thread_id)}", silent=True)
            return thread_id
        # The request itself, so the topic reads on its own.
        if message.get("message_id"):
            try:
                await self._tg.forward_message(chat_id, chat_id, message["message_id"], thread_id, silent=True)
            except TelegramError as exc:
                log.info("could not copy the request into its topic: %s", exc)
        # Housekeeping around the answer: the answer is the one message of the turn that rings.
        await self._tg.send_message(chat_id, f"Lo contesto en «{title}»: {topic_link(chat_id, thread_id)}", silent=True)
        return thread_id

    @staticmethod
    def _log_ignored(update: dict[str, Any], reason: str) -> None:
        """A message that was dropped, with the ids that decided it. Never the text.

        This is what tells you that `TELEGRAM_ADMIN_CHAT_ID` or `TELEGRAM_ALLOWED_USER_IDS` do not match: the ids
        here are the ones Telegram sent. Ids are not secrets.
        """
        message = update.get("message") or (update.get("callback_query") or {}).get("message")
        if message is None:
            log.debug("ignored: %s", reason)
            return
        sender = update.get("message", {}).get("from") or (update.get("callback_query") or {}).get("from") or {}
        log.info("ignored a message (%s): chat=%s user=%s", reason, (message.get("chat") or {}).get("id"), sender.get("id"))

    async def _on_message(self, message: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        thread_id = message.get("message_thread_id") if message.get("is_topic_message") else None
        if thread_id == GENERAL_TOPIC:
            thread_id = None
        # A file's text travels as its caption.
        text = (message.get("text") or message.get("caption") or "").strip()
        try:
            attachment = media.pick(message, self._s.voice_max_s)
        except media.Unreadable as exc:
            await self._tg.send_message(chat_id, str(exc), thread_id)
            return
        if not text and attachment is None:
            return

        lobby = thread_id is None and self._lobby_open()
        command = _command(text)
        if command in RUN_ALIASES:
            question = _arguments(text)
            if not question and attachment is None:
                # Tapping the command in Telegram's menu sends it bare: that is a way in, not a mistake.
                if lobby:
                    thread_id = await self._open_topic(chat_id, message, "")
                await self._tg.send_message(chat_id, READY, thread_id, silent=True)
                return
            if await self._over_budget(chat_id, thread_id):
                return
            if lobby:
                thread_id = await self._open_topic(chat_id, message, question or attachment.name)
            await self._ask(chat_id, user_id, thread_id, question, attachment)
            return
        if command is not None:
            self._audit.log("command", user=user_id, command=command)
            await self._run_command(command, chat_id, user_id, thread_id, lobby, _arguments(text))
            return

        if lobby:
            last = self._hinted_at.get(user_id)
            if last is None or self._clock() - last >= HINT_EVERY_S:
                self._hinted_at[user_id] = self._clock()
                await self._tg.send_message(chat_id, ASK_WITH_RUN)
            return
        if await self._over_budget(chat_id, thread_id):
            return
        if thread_id in self._untitled and text:
            self._untitled.discard(thread_id)
            self._remember_untitled()
            try:
                await self._tg.edit_forum_topic(chat_id, thread_id, topic_title(text))
            except TelegramError as exc:
                log.info("could not name topic %s: %s", thread_id, exc)
        await self._ask(chat_id, user_id, thread_id, text, attachment)

    async def _listen(self, attachment: media.Attachment, data: bytes) -> str:
        """The text of a voice note. Raises `media.Unreadable` with the line for the admin when there is none."""
        if self._transcriber is None:
            raise media.Unreadable(media.NO_VOICE_HERE)
        if len(data) > media.MAX_VOICE_BYTES:
            raise media.Unreadable(media.VOICE_TOO_LONG.format(limit=self._s.voice_max_s))
        try:
            heard = await self._transcriber.transcribe(data)
        except voice.NotReady:
            raise media.Unreadable(media.VOICE_NOT_READY) from None
        except voice.VoiceError as exc:
            log.warning("could not transcribe a voice note: %s", exc)
            raise media.Unreadable(media.NOT_UNDERSTOOD) from None
        if not heard:
            raise media.Unreadable(media.NOT_UNDERSTOOD)
        return heard

    async def _over_budget(self, chat_id: int, thread_id: Optional[int]) -> bool:
        """Say so and return True when today's allowance is gone. Checked before a topic is opened."""
        if self._budget is None or not self._budget.exhausted():
            return False
        await self._tg.send_message(chat_id, OVER_BUDGET.format(limit=self._budget.limit_usd), thread_id)
        return True

    async def _ask(
        self,
        chat_id: int,
        user_id: int,
        thread_id: Optional[int],
        text: str,
        attachment: Optional[media.Attachment] = None,
    ) -> None:
        # What the tools and the model produce during the turn. It goes out once, at the end, as few messages as
        # hold it, and only the last one makes the admins' phones ring.
        outbox: list[tuple[str, list[OutFile], Optional[list[tuple[str, str]]]]] = []

        async def collect(
            reply: str, files: Optional[list[OutFile]] = None, buttons: Optional[list[tuple[str, str]]] = None
        ) -> None:
            outbox.append((reply, list(files or []), buttons))
            await self._tg.send_chat_action(chat_id, thread_id)

        async def say(reply: str, silent: bool = False) -> None:
            await self._tg.send_message(chat_id, reply, thread_id, silent=silent)

        content: Any = text
        if attachment is not None:
            try:
                data = await self._tg.download(attachment.file_id)
                if attachment.kind == "voice":
                    heard = await self._listen(attachment, data)
                    # The admin sees what was understood before the answer: a misheard name or number shows here.
                    await say(media.HEARD.format(text=heard), silent=True)
                    content = text = f"{text}\n\n{heard}".strip()
                else:
                    content = [media.to_block(attachment, data), {"type": "text", "text": text or media.DEFAULT_QUESTION}]
            except TelegramError as exc:
                log.info("could not download a file: %s", exc)
                await say(DOWNLOAD_FAILED)
                return
            except media.Unreadable as exc:
                await say(str(exc))
                return

        session = await self._open(user_id, thread_id or 0)
        async with session.lock:
            before = cost_usd(self._s.model, session.usage) or 0.0
            if session.context_tokens > self._s.session_max_tokens:
                await say(COMPACTING, silent=True)
                if not await self._agent.compact(session):
                    session.reset()
                    await say(FRESH_SESSION, silent=True)
            await self._tg.send_chat_action(chat_id, thread_id)
            # What kind of file and how big, never its name or what it says.
            sent = {"file": attachment.kind, "bytes": attachment.size} if attachment else {}
            if attachment and attachment.kind == "voice":
                sent["seconds"] = attachment.seconds
            self._audit.log("message", user=user_id, chars=len(text), topic=thread_id or 0, **sent)
            answer = await self._agent.run(session, content, user_id, collect)
            reached = False
            if self._budget is not None:
                self._budget.add((cost_usd(self._s.model, session.usage) or 0.0) - before)
                reached = self._budget.exhausted()
            await self._save(user_id, thread_id or 0)
        if answer:
            outbox.append((answer, [], None))
        if reached:
            log.warning("the daily budget of $%.2f was reached", self._budget.limit_usd)
            outbox.append((BUDGET_REACHED.format(limit=self._budget.limit_usd), [], None))
        await self._flush(chat_id, thread_id, outbox)

    async def _flush(
        self,
        chat_id: int,
        thread_id: Optional[int],
        outbox: list[tuple[str, list[OutFile], Optional[list[tuple[str, str]]]]],
    ) -> None:
        """One turn's output: the pictures as one album, the files, the text, then each card with buttons."""
        plain = [(text, files) for text, files, buttons in outbox if not buttons]
        photos = [f for _, files in plain for f in files if f.photo]
        documents = [f for _, files in plain for f in files if not f.photo]
        text = "\n\n".join(t for t, _ in plain if t)
        # Short enough, the text rides under the last picture or file instead of being a message of its own.
        caption = text if (photos or documents) and len(text) <= MAX_CAPTION else ""

        sends: list[Callable[[bool], Awaitable[None]]] = []

        def album(pictures: list[OutFile], under: str) -> Callable[[bool], Awaitable[None]]:
            if len(pictures) == 1:
                only = pictures[0]
                return lambda silent: self._tg.send_photo(chat_id, only.name, only.content, under, thread_id, only.mime, silent=silent)
            return lambda silent: self._tg.send_photos(chat_id, [(f.name, f.content) for f in pictures], under, thread_id, silent=silent)

        def document(file: OutFile, under: str) -> Callable[[bool], Awaitable[None]]:
            return lambda silent: self._tg.send_document(chat_id, file.name, file.content, under, thread_id, silent=silent)

        def message(body: str, buttons: Optional[list[tuple[str, str]]] = None) -> Callable[[bool], Awaitable[None]]:
            return lambda silent: self._tg.send_message(chat_id, body, thread_id, _keyboard(buttons), silent=silent)

        if photos:
            sends.append(album(photos, "" if documents else caption))
        for n, file in enumerate(documents, 1):
            sends.append(document(file, caption if n == len(documents) else ""))
        if text and not caption:
            sends.append(message(text))
        for body, files, buttons in outbox:
            if not buttons:
                continue
            # Buttons only hang from a text message, and that message is the one rewritten once they are
            # pressed: the card's own files go first, bare, and its text follows with the keyboard.
            pictures = [f for f in files if f.photo]
            if pictures:
                sends.append(album(pictures, ""))
            sends.extend(document(f, "") for f in files if not f.photo)
            sends.append(message(body, buttons))

        for n, send in enumerate(sends, 1):
            await send(n < len(sends))

    async def _run_command(
        self, command: str, chat_id: int, user_id: int, thread_id: Optional[int], lobby: bool, arguments: str = ""
    ) -> None:
        async def say(reply: str, buttons: Optional[list[tuple[str, str]]] = None) -> None:
            await self._tg.send_message(chat_id, reply, thread_id, _keyboard(buttons))

        session = await self._open(user_id, thread_id or 0)
        if command in ("help", "start"):
            await say(HELP)
        elif command == "clear":
            if lobby:
                await say(IN_LOBBY_NEW)
            else:
                session.reset()
                await self._save(user_id, thread_id or 0)
                await say("Listo, sesión limpia.")
        elif command == "usage":
            if lobby:
                await say(IN_LOBBY_USAGE)
            else:
                fixed = await self._agent.fixed_tokens() if session.context_tokens else None
                await say(
                    usage_report(self._s.model, self._s.session_max_tokens, session.context_tokens, session.usage, fixed)
                )
        elif command == "prefs" and self._preferences is not None:
            rules = self._preferences.rules()
            await say("\n".join(f"{i}. {rule}" for i, rule in enumerate(rules, 1)) if rules else NO_PREFS)
        elif command == "forget" and self._preferences is not None and self._confirmations is not None:
            lines = self._preferences.lines()
            if not arguments.isdigit() or not 1 <= int(arguments) <= len(lines):
                await say(FORGET_NEEDS_NUMBER if lines else NO_PREFS)
                return
            line = lines[int(arguments) - 1]
            summary = f"Voy a olvidar esta regla permanente:\n«{self._preferences.rules()[int(arguments) - 1]}»"
            action_id = await self._confirmations.propose(user_id, "preference_remove", {"line": line}, summary)
            await say(summary, confirmations.buttons(action_id, "Olvidar"))
        else:
            await say(UNKNOWN_COMMAND)

    async def run(self) -> None:
        offset: Optional[int] = None
        tasks: set[asyncio.Task[None]] = set()
        log.info("Duma is listening")
        while True:
            try:
                updates = await self._tg.get_updates(offset)
            except TelegramError as exc:
                log.warning("polling failed: %s", exc)
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                task = asyncio.create_task(self._safe(update))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            try:
                await self.sweep()
            except Exception as exc:
                log.warning("sweep failed: %s", type(exc).__name__)

    async def _safe(self, update: dict[str, Any]) -> None:
        try:
            await self.handle(update)
        except Exception as exc:  # one bad update must not stop the polling
            log.warning("update failed: %s", type(exc).__name__)


async def open_database(settings: Settings) -> Database:
    if not settings.database_url:
        db: Database = SqliteDatabase(settings.state_dir / "bot.sqlite")
        log.warning("no BOT_DATABASE_URL and no CLOUDRON_POSTGRESQL_URL: Duma keeps its state in a %s", db.name)
        return db
    db = await PostgresDatabase.connect(settings.database_url, settings.database_schema)
    log.info("state in %s", db.name)
    shared = await db.foreign_tables()
    if shared:
        log.warning(
            "the database user can read %d table(s) outside schema %s: it is not a role of Duma's own", shared, db.schema
        )
    return db


async def amain() -> int:
    setup_logging()
    try:
        settings = load()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    telegram = Telegram(settings.telegram_bot_token)
    api = MuunganoApi(settings.api_url, settings.api_token, settings.api_timeout_s, settings.api_max_per_min)
    audit = Audit(settings.state_dir / "audit.log")
    budget = Budget(settings.state_dir / "budget.json", settings.daily_budget_usd, settings.tz)
    try:
        db = await open_database(settings)
    except DatabaseError as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        return 2
    pending = await Confirmations.open(db, settings.confirm_ttl_min * 60)
    store = await SessionStore.open(db)
    preferences = Preferences(settings.state_dir / "preferencias.md", settings.tz)
    transcriber = None
    if voice.installed():
        transcriber = voice.Transcriber(
            settings.voice_model, settings.voice_dir or settings.state_dir / "whisper", settings.voice_threads
        )
    agent = Agent.from_prompt_file(
        settings,
        AnthropicLLM(settings.anthropic_api_key),
        Toolbox(api, pending, preferences),
        PROMPT,
        audit,
        extra_system=preferences.for_prompt,
    )
    try:
        bot = Bot(
            settings, telegram, agent, audit, api=api, budget=budget, confirmations=pending, preferences=preferences, store=store,
            transcriber=transcriber,
        )
        await bot.check()
        if settings.hook_port:
            try:
                await hooks.serve(settings.hook_port, settings.api_token, bot.on_hook)
            except OSError as exc:
                log.warning("not listening for the API's hooks on port %d: %s", settings.hook_port, exc)
        await bot.run()
    finally:
        await telegram.aclose()
        await api.aclose()
        await db.close()
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
