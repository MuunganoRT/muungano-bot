"""Sessions on disk, so a restart does not forget a topic's conversation.

One row per (admin, topic), next to the confirmations. A row holds what the
model was sent and what it answered, the codes that stand for people's names,
and what the session has cost; a session nobody has touched for a while is
deleted, not kept.
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Callable, Optional

from duma.agent import Session
from duma.database import Database
from duma.pseudonyms import Pseudonyms
from duma.usage import Usage


def _dump(session: Session) -> str:
    return json.dumps(
        {
            "messages": session.messages,
            "context_tokens": session.context_tokens,
            "turns": session.turns,
            "usage": dataclasses.asdict(session.usage),
            "notes": session.notes,
            "names": session.names.to_list(),
            "prefix": session.prefix,
        },
        ensure_ascii=False,
    )


def _load(raw: str) -> Session:
    data = json.loads(raw)
    return Session(
        messages=data["messages"],
        context_tokens=data["context_tokens"],
        turns=data["turns"],
        usage=Usage(**data["usage"]),
        notes=data["notes"],
        names=Pseudonyms.from_list(data["names"]),
        # Absent in a session saved before the field existed: empty never matches, so its thinking is dropped.
        prefix=data.get("prefix", ""),
    )


class SessionStore:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time):
        self._db = db
        self._clock = clock

    @classmethod
    async def open(cls, db: Database, clock: Callable[[], float] = time.time) -> "SessionStore":
        # `data` is TEXT and not JSONB on purpose: the history must go back to the model byte for byte, and
        # JSONB reorders the keys of what it stores.
        await db.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "user_id BIGINT NOT NULL, thread_id BIGINT NOT NULL, data TEXT NOT NULL, "
            "updated_at DOUBLE PRECISION NOT NULL, PRIMARY KEY (user_id, thread_id))"
        )
        return cls(db, clock)

    async def save(self, user_id: int, thread_id: int, session: Session) -> None:
        await self._db.execute(
            "INSERT INTO sessions (user_id, thread_id, data, updated_at) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (user_id, thread_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
            user_id, thread_id, _dump(session), self._clock(),
        )

    async def load(self, user_id: int, thread_id: int) -> Optional[Session]:
        rows = await self._db.fetch("SELECT data FROM sessions WHERE user_id = $1 AND thread_id = $2", user_id, thread_id)
        if not rows:
            return None
        try:
            return _load(rows[0][0])
        except (ValueError, KeyError, TypeError):
            # Written by an older build with another shape: a fresh session beats a crash on every message.
            return None

    async def delete_topic(self, thread_id: int) -> None:
        await self._db.execute("DELETE FROM sessions WHERE thread_id = $1", thread_id)

    async def idle_topics(self, seconds: float) -> list[int]:
        """Topics where every admin's session has been untouched for that long."""
        rows = await self._db.fetch(
            "SELECT thread_id FROM sessions GROUP BY thread_id HAVING MAX(updated_at) < $1", self._clock() - seconds
        )
        return [thread_id for (thread_id,) in rows]
