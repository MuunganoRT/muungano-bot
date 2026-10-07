"""Who Duma listens to.

Two checks, both required: the chat is the admin group AND the sender is on the
allow-list. Anything else is ignored without an answer, and a group that is not
the admin one gets the bot removed from it.

Pure functions over the raw Telegram update, so they can be tested without a
network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Optional

from duma.config import Settings

Action = Literal["handle", "ignore", "leave", "migrated"]

GROUP_TYPES = {"group", "supergroup", "channel"}
JOINED = {"member", "administrator"}


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    # `leave`: the chat to leave. `migrated`: the new id of the admin group.
    chat_id: Optional[int] = None


def decide(update: dict[str, Any], settings: Settings) -> Decision:
    member = update.get("my_chat_member")
    if member:
        chat = member.get("chat", {})
        status = (member.get("new_chat_member") or {}).get("status")
        if chat.get("id") != settings.admin_chat_id and chat.get("type") in GROUP_TYPES and status in JOINED:
            return Decision("leave", "added to a chat that is not the admin group", chat.get("id"))
        return Decision("ignore", "membership change")

    message = update.get("message")
    callback = update.get("callback_query")
    if message is None and callback is not None:
        message = callback.get("message") or {}
        sender = callback.get("from") or {}
    elif message is not None:
        sender = message.get("from") or {}
    else:
        return Decision("ignore", "not a message")

    chat = message.get("chat") or {}
    if chat.get("id") != settings.admin_chat_id:
        return Decision("ignore", "not the admin group")

    if message.get("migrate_to_chat_id"):
        return Decision("migrated", "the admin group became a supergroup", message["migrate_to_chat_id"])

    if sender.get("is_bot"):
        return Decision("ignore", "sender is a bot")
    if sender.get("id") not in settings.allowed_user_ids:
        return Decision("ignore", "sender is not allowed")

    return Decision("handle", "ok")
