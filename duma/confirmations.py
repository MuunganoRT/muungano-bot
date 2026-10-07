"""Actions with an effect wait here until an admin presses a button.

The model can only PROPOSE: what will run is stored at that moment, the button
carries nothing but the id, and the text shown to the admin is the one stored
with it. So what was shown is what runs, whatever the model says afterwards.

Three rules, enforced in `claim`: only the admin who asked can decide, a
proposal is decided once, and it expires.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from duma.database import Database

Status = Literal["ok", "gone", "expired", "not_yours"]

CONFIRM, CANCEL = "ok", "no"


@dataclass(frozen=True)
class Pending:
    id: str
    user_id: int
    kind: str
    payload: dict[str, Any]
    # What the admin was shown. The message is rewritten from this once it is decided.
    summary: str


def buttons(action_id: str, confirm_label: str) -> list[tuple[str, str]]:
    """(label, callback data) pairs for the two buttons of a proposal."""
    return [(confirm_label, f"{CONFIRM}:{action_id}"), ("Cancelar", f"{CANCEL}:{action_id}")]


def parse(data: str) -> Optional[tuple[str, str]]:
    """`ok:<id>` -> (`ok`, id); None for anything that is not one of this module's buttons."""
    verb, _, action_id = (data or "").partition(":")
    return (verb, action_id) if verb in (CONFIRM, CANCEL) and action_id else None


class Confirmations:
    def __init__(self, db: Database, ttl_s: int, clock: Callable[[], float] = time.time):
        self._db = db
        self._ttl_s = ttl_s
        self._clock = clock

    @classmethod
    async def open(cls, db: Database, ttl_s: int, clock: Callable[[], float] = time.time) -> "Confirmations":
        await db.execute(
            "CREATE TABLE IF NOT EXISTS pending ("
            "id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, "
            "summary TEXT NOT NULL, expires_at DOUBLE PRECISION NOT NULL, decided INTEGER NOT NULL DEFAULT 0)"
        )
        return cls(db, ttl_s, clock)

    async def propose(self, user_id: int, kind: str, payload: dict[str, Any], summary: str) -> str:
        action_id = secrets.token_urlsafe(12)
        await self._db.execute(
            "INSERT INTO pending (id, user_id, kind, payload, summary, expires_at) VALUES ($1, $2, $3, $4, $5, $6)",
            action_id, user_id, kind, json.dumps(payload, ensure_ascii=False), summary, self._clock() + self._ttl_s,
        )
        return action_id

    async def purge(self, older_than_s: float = 86400) -> None:
        """Forget proposals that expired that long ago: decided or not, nothing can use them any more."""
        await self._db.execute("DELETE FROM pending WHERE expires_at < $1", self._clock() - older_than_s)

    async def claim(self, action_id: str, user_id: int) -> tuple[Status, Optional[Pending]]:
        """Take a proposal to decide it. `ok` is returned once; someone else's click consumes nothing."""
        rows = await self._db.fetch(
            "SELECT user_id, kind, payload, summary, expires_at, decided FROM pending WHERE id = $1", action_id
        )
        if not rows:
            return "gone", None
        owner, kind, payload, summary, expires_at, decided = rows[0]
        pending = Pending(action_id, owner, kind, json.loads(payload), summary)
        if owner != user_id:
            return "not_yours", None
        if decided:
            return "gone", pending
        # The UPDATE is the lock: of two clicks that arrive together, only one changes the row.
        changed = await self._db.execute("UPDATE pending SET decided = 1 WHERE id = $1 AND decided = 0", action_id)
        if not changed:
            return "gone", pending
        if self._clock() > expires_at:
            return "expired", pending
        return "ok", pending
