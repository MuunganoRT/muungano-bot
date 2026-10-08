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
# The button shown while a decision waits to run.
STOP = "stop"
# The owner of a proposal Duma posted on its own: whoever the chat lets click.
ANYONE = 0


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


RECEIPT = "rc"
CLOSE = "x"
# What the member is told by e-mail, word for word.
REJECTIONS = {
    "ri": ("Ilegible", "La foto no se alcanza a leer. Sube una imagen más clara de tu comprobante."),
    "rm": ("Monto no coincide", "El monto del comprobante no coincide con el plan que elegiste."),
    "rn": ("No es un comprobante", "El archivo que subiste no es un comprobante de pago."),
    "rb": ("Beneficio no válido", "No pudimos validar tu beneficio con esa imagen."),
}


def receipt_buttons(action_id: str, months: list[int], asked: Optional[int], benefit: bool) -> list[tuple[str, str]]:
    """The buttons of a receipt card: one per plan, the reasons to reject, and one to leave it as it is."""
    out = []
    for n in months:
        label = f"{n} mes" if n == 1 else f"{n} meses"
        out.append((f"Aprobar {label}" if n == asked else label, f"{RECEIPT}:a{n}:{action_id}"))
    for code in ("ri", "rb") if benefit else ("ri", "rm", "rn"):
        out.append((f"Rechazar: {REJECTIONS[code][0].lower()}", f"{RECEIPT}:{code}:{action_id}"))
    out.append(("Dejar pendiente", f"{RECEIPT}:{CLOSE}:{action_id}"))
    return out


def parse_receipt(data: str) -> Optional[tuple[str, str]]:
    """`rc:a3:<id>` -> (`a3`, id); None for anything that is not a button of a receipt card."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != RECEIPT or not parts[2]:
        return None
    code = parts[1]
    known = code == CLOSE or code in REJECTIONS or (code[:1] == "a" and code[1:].isdigit())
    return (code, parts[2]) if known else None


def decision(code: str) -> Optional[dict[str, Any]]:
    """What a button of a receipt card asks the API to do. None for the one that leaves it pending."""
    if code in REJECTIONS:
        return {"accion": "rechazar", "motivo": REJECTIONS[code][1]}
    if code[:1] == "a" and code[1:].isdigit():
        return {"accion": "aprobar", "meses": int(code[1:])}
    return None


APPLICATION = "ap"
# Button code -> what the API is asked to do, and how the outcome reads.
APPLICATION_ACTIONS = {"a": ("accept", "Aceptada"), "w": ("wait", "En lista de espera"), "r": ("reject", "Rechazada")}


def application_buttons(action_id: str, can_accept: bool) -> list[tuple[str, str]]:
    """The buttons of an application card: the console's three, and one to leave it as it is."""
    out = [("Aceptar", f"{APPLICATION}:a:{action_id}")] if can_accept else []
    out += [("Lista de espera", f"{APPLICATION}:w:{action_id}"), ("Rechazar", f"{APPLICATION}:r:{action_id}")]
    return [*out, ("Dejar pendiente", f"{APPLICATION}:{CLOSE}:{action_id}")]


def parse_application(data: str) -> Optional[tuple[str, str]]:
    """`ap:a:<id>` -> (`a`, id); None for anything that is not a button of an application card."""
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != APPLICATION or not parts[2]:
        return None
    return (parts[1], parts[2]) if parts[1] == CLOSE or parts[1] in APPLICATION_ACTIONS else None


def parse_stop(data: str) -> Optional[str]:
    """`stop:<id>` -> id; None for any other button."""
    verb, _, action_id = (data or "").partition(":")
    return action_id if verb == STOP and action_id else None


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

    async def propose(
        self, user_id: int, kind: str, payload: dict[str, Any], summary: str, ttl_s: Optional[int] = None
    ) -> str:
        """Store a proposal. `user_id` ANYONE is one nobody asked for: any admin may decide it."""
        action_id = secrets.token_urlsafe(12)
        await self._db.execute(
            "INSERT INTO pending (id, user_id, kind, payload, summary, expires_at) VALUES ($1, $2, $3, $4, $5, $6)",
            action_id, user_id, kind, json.dumps(payload, ensure_ascii=False), summary, self._clock() + (ttl_s or self._ttl_s),
        )
        return action_id

    async def purge(self, older_than_s: float = 86400) -> None:
        """Forget proposals that expired that long ago: decided or not, nothing can use them any more."""
        await self._db.execute("DELETE FROM pending WHERE expires_at < $1", self._clock() - older_than_s)

    async def release(self, action_id: str) -> None:
        """Give back a proposal whose decision was called off before it ran: it can be decided again."""
        await self._db.execute("UPDATE pending SET decided = 0 WHERE id = $1", action_id)

    async def claim(self, action_id: str, user_id: int) -> tuple[Status, Optional[Pending]]:
        """Take a proposal to decide it. `ok` is returned once; someone else's click consumes nothing."""
        rows = await self._db.fetch(
            "SELECT user_id, kind, payload, summary, expires_at, decided FROM pending WHERE id = $1", action_id
        )
        if not rows:
            return "gone", None
        owner, kind, payload, summary, expires_at, decided = rows[0]
        pending = Pending(action_id, owner, kind, json.loads(payload), summary)
        if owner not in (user_id, ANYONE):
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
