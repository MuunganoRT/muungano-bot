"""One conversation turn: the model, its tools and the chat in between.

The loop is the manual tool-use loop of the Messages API: call the model, run
every `tool_use` block it asks for, send ALL the results back in one user
message, repeat until it answers in text.

Three rules that keep the history valid and the model's reasoning intact:
  - the assistant's `content` is appended with every block as received,
    thinking blocks included (as plain dicts, so a session can be saved), and
    earlier messages are never edited (the history only grows);
  - a thinking block is only valid after the exact instructions and tools it
    was written under. When those change under a saved session (a deploy, a new
    preference, the date in the prompt), its thinking blocks are removed once,
    before the next request, instead of sending blocks the API will refuse;
  - when a turn fails half way, every message it added is removed, so the next
    request never starts from a `tool_use` without its `tool_result`;
  - a session that grows past its limit is replaced whole, never trimmed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Protocol
from zoneinfo import ZoneInfo

from duma.audit import Audit
from duma.config import Settings
from duma.pseudonyms import Pseudonyms
from duma.tools import Toolbox
from duma.usage import Usage

log = logging.getLogger(__name__)

# Called as send(text) or, when a tool attached something, send(text, file=..., buttons=...).
Send = Callable[..., Awaitable[None]]

TRY_AGAIN = "No pude con eso ahorita. Intenta de nuevo en un momento."
TOO_MANY_STEPS = "Me enredé con tantos pasos. ¿Me lo pides más puntual?"
REFUSED = "Eso no te lo puedo contestar."
CUT_OFF = "(Me corté. Pídeme una versión más corta.)"

SUMMARY_REQUEST = (
    "Esta conversación se va a compactar: vas a continuar solo con las notas que escribas ahora. Escríbelas en viñetas "
    "breves, sin saludo ni cierre: qué ha pedido el administrador; qué se consultó, con qué filtros y periodos; quién "
    "o qué es «el actual» (atleta, grupo, evento, periodo) para entender preguntas como «¿y los del otro grupo?»; y qué "
    "quedó pendiente. Usa los códigos ATLETA_NN tal cual. No inventes cifras ni llames herramientas."
)
NOTES_HEADER = "Notas de esta conversación hasta aquí (la sesión se compactó; continúa a partir de ellas):"


class LLM(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...

    async def count(self, **kwargs: Any) -> int: ...


class AnthropicLLM:
    """The Anthropic SDK behind the small interface the agent needs."""

    def __init__(self, api_key: str):
        import anthropic  # imported here so the rest of the bot can be tested without the SDK

        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def create(self, **kwargs: Any) -> Any:
        return await self._client.messages.create(**kwargs)

    async def count(self, **kwargs: Any) -> int:
        return (await self._client.messages.count_tokens(**kwargs)).input_tokens


@dataclass
class Session:
    messages: list[Any] = field(default_factory=list)
    # Size of the context the last request carried, as the API reported it.
    context_tokens: int = 0
    turns: int = 0
    # Everything this session has spent. A reset clears the conversation, not what it already cost.
    usage: Usage = field(default_factory=Usage)
    # What the model wrote down when the conversation was compacted. It opens the next request.
    notes: str = ""
    # Who each ATLETA_NN is. Kept across a compaction, so the notes' codes still resolve.
    names: Pseudonyms = field(default_factory=Pseudonyms)
    # Fingerprint of the instructions and tools the thinking blocks in `messages` were written under.
    prefix: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def reset(self) -> None:
        self.messages.clear()
        self.context_tokens = 0
        self.turns = 0
        self.notes = ""
        self.names = Pseudonyms()


def _usage_total(response: Any) -> int:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0
    return sum(
        getattr(usage, name, 0) or 0
        for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )


def _plain(block: Any) -> Any:
    """A content block as the plain dict the API takes back, so the history can be saved and reloaded as it is.

    Only the fields the API sent: a reasoning block has to return exactly as it came, and a field the API did not
    send would not be the block it signed.
    """
    dump = getattr(block, "model_dump", None)
    if dump is None:
        return dict(vars(block))
    return dump(mode="json", exclude_unset=True, by_alias=True)


def _text(content: list[Any]) -> str:
    return "".join(block.text for block in content if block.type == "text").strip()


class Agent:
    def __init__(
        self,
        settings: Settings,
        llm: LLM,
        toolbox: Toolbox,
        system_prompt: str,
        audit: Optional[Audit] = None,
        extra_system: Optional[Callable[[], str]] = None,
    ):
        # Text added after the instructions on every request; today, the admins' standing preferences.
        self._extra_system = extra_system
        self._s = settings
        self._llm = llm
        self._tools = toolbox
        self._prompt = system_prompt
        self._audit = audit
        self._fixed: dict[str, tuple[int, int]] = {}

    @classmethod
    def from_prompt_file(
        cls,
        settings: Settings,
        llm: LLM,
        toolbox: Toolbox,
        path: Path,
        audit: Optional[Audit] = None,
        extra_system: Optional[Callable[[], str]] = None,
    ) -> "Agent":
        return cls(settings, llm, toolbox, Path(path).read_text(encoding="utf-8"), audit, extra_system)

    def _system_blocks(self) -> list[dict[str, Any]]:
        """The instructions, cached, and after them whatever changes more often than they do.

        The preferences go after the cache point on purpose: saving one must not throw away the cached
        instructions and tools that every topic shares.
        """
        blocks: list[dict[str, Any]] = [{"type": "text", "text": self._system(), "cache_control": {"type": "ephemeral"}}]
        extra = self._extra_system() if self._extra_system else ""
        if extra:
            blocks.append({"type": "text", "text": extra})
        return blocks

    def _system(self) -> str:
        now = datetime.now(ZoneInfo(self._s.tz))
        return f"{self._prompt}\n\nHoy es {now:%Y-%m-%d} ({self._s.tz})."

    async def fixed_tokens(self) -> Optional[tuple[int, int]]:
        """Tokens of the instructions and of the tool definitions: what every request carries before the conversation.

        Counted by the API (the count is free) and kept until the prompt changes, which it does once a day.
        """
        system = self._system()
        if system not in self._fixed:
            probe = [{"role": "user", "content": "."}]
            model = self._s.model
            try:
                bare, with_system, with_tools = await asyncio.gather(
                    self._llm.count(model=model, messages=probe),
                    self._llm.count(model=model, system=system, messages=probe),
                    self._llm.count(model=model, system=system, tools=self._tools.schemas, messages=probe),
                )
            except Exception as exc:
                log.info("could not count the fixed tokens: %s", type(exc).__name__)
                return None
            self._fixed = {system: (with_system - bare, with_tools - with_system)}
        return self._fixed[system]

    def _align(self, session: Session, system: list[dict[str, Any]]) -> None:
        """Drop the session's thinking blocks if they were written under other instructions or tools."""
        prefix = hashlib.sha256(
            json.dumps([system, self._tools.schemas], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        if session.prefix == prefix:
            return
        for message in session.messages:
            content = message.get("content")
            if message.get("role") != "assistant" or not isinstance(content, list):
                continue
            kept = [b for b in content if b.get("type") not in ("thinking", "redacted_thinking")]
            # An assistant turn cannot be empty, and removing the message would break the user/assistant order.
            message["content"] = kept or [{"type": "text", "text": "(sin texto)"}]
        session.prefix = prefix

    async def compact(self, session: Session) -> bool:
        """Replace the conversation with the model's own notes of it. False when the notes could not be written.

        The history is replaced whole and the notes open a new one: a history with turns cut out of the middle
        would carry reasoning blocks that no longer match what came before them, and the API rejects that.
        """
        if not session.messages:
            return False
        system = self._system_blocks()
        self._align(session, system)
        try:
            response = await self._llm.create(
                model=self._s.model,
                max_tokens=self._s.max_tokens_per_message,
                system=system,
                cache_control={"type": "ephemeral"},
                tools=self._tools.schemas,
                messages=[*session.messages, {"role": "user", "content": SUMMARY_REQUEST}],
                output_config={"effort": self._s.effort},
            )
        except Exception as exc:
            log.warning("compaction failed: %s", type(exc).__name__)
            return False
        session.usage.add(response)
        notes = _text(response.content)
        if response.stop_reason != "end_turn" or not notes:
            log.warning("compaction gave no notes (stop reason: %s)", response.stop_reason)
            return False
        session.messages.clear()
        session.context_tokens = 0
        session.notes = notes
        return True

    @staticmethod
    def _opening(session: Session, content: Any) -> Any:
        """The admin's message, preceded by the notes when it is the first one after a compaction."""
        if session.messages or not session.notes:
            return content
        notes = f"{NOTES_HEADER}\n{session.notes}\n\n---\n\n"
        if isinstance(content, str):
            return notes + content
        return [{"type": "text", "text": notes.rstrip()}, *content]

    async def run(self, session: Session, content: Any, telegram_user_id: int, send: Send) -> Optional[str]:
        """Answer one admin message: text, or a list of content blocks when it came with a file.

        Returns the text for the chat, or None if there is none.
        """
        # Read once per turn: the date in it must not change between two requests of the same tool loop.
        system = self._system_blocks()
        self._align(session, system)
        mark = len(session.messages)
        session.messages.append({"role": "user", "content": self._opening(session, content)})
        try:
            for _ in range(self._s.max_turns):
                response = await self._llm.create(
                    model=self._s.model,
                    max_tokens=self._s.max_tokens_per_message,
                    # Two cache points: the instructions and tools, which every topic and admin share, and
                    # (the top-level one) the end of this conversation, which moves forward with each request.
                    system=system,
                    cache_control={"type": "ephemeral"},
                    tools=self._tools.schemas,
                    messages=session.messages,
                    output_config={"effort": self._s.effort},
                )
                session.usage.add(response)
                session.context_tokens = _usage_total(response)
                session.messages.append({"role": "assistant", "content": [_plain(b) for b in response.content]})

                if response.stop_reason == "tool_use":
                    results = []
                    for block in response.content:
                        if block.type != "tool_use":
                            continue
                        result = await self._tools.run(block.name, dict(block.input), telegram_user_id, session.names)
                        if self._audit:
                            self._audit.log("tool", user=telegram_user_id, tool=block.name, args=dict(block.input), error=result.is_error)
                        if result.file or result.buttons:
                            await send(result.direct_text or "", file=result.file, buttons=result.buttons)
                        elif result.direct_text:
                            await send(result.direct_text)
                        item: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id, "content": result.to_model}
                        if result.is_error:
                            item["is_error"] = True
                        results.append(item)
                    session.messages.append({"role": "user", "content": results})
                    continue

                session.turns += 1
                if response.stop_reason == "refusal":
                    return REFUSED
                answer = session.names.restore(_text(response.content))
                if response.stop_reason == "max_tokens":
                    answer = f"{answer}\n{CUT_OFF}".strip()
                return answer or None

            del session.messages[mark:]
            return TOO_MANY_STEPS
        except Exception as exc:  # the model, the network or a tool broke: never leave a half turn behind
            del session.messages[mark:]
            log.warning("turn failed: %s", type(exc).__name__)
            return TRY_AGAIN
