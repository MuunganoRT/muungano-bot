"""Minimal Telegram Bot API client.

The token is part of every URL, so two things matter here: errors never carry
the URL (only the method and Telegram's own description), and nothing in this
module logs one. `httpx` logs every request line at INFO, which would put the
token in the log; `main.setup_logging` raises that logger's level.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import httpx

# Telegram rejects messages over 4096 characters; stay under it.
MAX_MESSAGE = 4000
# A file's caption is capped at 1024; a longer text goes as its own message first.
MAX_CAPTION = 1000
# What Telegram takes in one album.
MAX_ALBUM = 10
CALLBACK_MAX = 200


class TelegramError(Exception):
    """A failed call. Carries the method and Telegram's description, never the URL."""

    def __init__(self, method: str, status: Optional[int], description: str):
        super().__init__(f"Telegram {method}: {status} {description}")
        self.method, self.status, self.description = method, status, description


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Pieces of at most `limit` characters, cut at a newline when there is one."""
    text = text.strip()
    if not text:
        return []
    pieces: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:  # no useful line break: cut hard
            cut = limit
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text:
        pieces.append(text)
    return pieces


class Telegram:
    def __init__(self, token: str, client: Optional[httpx.AsyncClient] = None):
        self._base = f"https://api.telegram.org/bot{token}/"
        self._files = f"https://api.telegram.org/file/bot{token}/"
        self._http = client or httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0))

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        files: Optional[dict[str, Any]] = None,
    ) -> Any:
        try:
            if files:
                response = await self._http.post(self._base + method, data=params or {}, files=files, timeout=timeout)
            else:
                response = await self._http.post(self._base + method, json=params or {}, timeout=timeout)
        except httpx.HTTPError as exc:
            # The exception text can contain the URL: keep only its class.
            raise TelegramError(method, None, type(exc).__name__) from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(method, response.status_code, "response is not JSON") from None
        if not body.get("ok"):
            raise TelegramError(method, response.status_code, str(body.get("description", ""))[:160])
        return body["result"]

    async def get_updates(self, offset: Optional[int], timeout: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query", "my_chat_member"],
        }
        if offset is not None:
            params["offset"] = offset
        # The HTTP timeout has to outlast Telegram's long poll.
        return await self._call("getUpdates", params, timeout=timeout + 10)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        thread_id: Optional[int] = None,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> None:
        pieces = split_message(text)
        for i, piece in enumerate(pieces):
            params: dict[str, Any] = {"chat_id": chat_id, "text": piece, "disable_web_page_preview": True}
            if thread_id:
                params["message_thread_id"] = thread_id
            if reply_markup and i == len(pieces) - 1:
                params["reply_markup"] = reply_markup
            await self._call("sendMessage", params)

    async def send_document(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        caption: str = "",
        thread_id: Optional[int] = None,
    ) -> None:
        if len(caption) > MAX_CAPTION:
            await self.send_message(chat_id, caption, thread_id)
            caption = ""
        params: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption
        if thread_id:
            params["message_thread_id"] = thread_id
        await self._call("sendDocument", params, files={"document": (filename, content)})

    async def send_photo(
        self,
        chat_id: int,
        filename: str,
        content: bytes,
        caption: str = "",
        thread_id: Optional[int] = None,
        mime: str = "image/png",
    ) -> None:
        """A picture shown in the chat itself. Telegram recompresses it; `send_document` keeps the original."""
        params: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption[:MAX_CAPTION]
        if thread_id:
            params["message_thread_id"] = thread_id
        await self._call("sendPhoto", params, files={"photo": (filename, content, mime)})

    async def send_photos(
        self,
        chat_id: int,
        photos: list[tuple[str, bytes]],
        caption: str = "",
        thread_id: Optional[int] = None,
    ) -> None:
        """Several pictures as one album, in order. More than an album holds go as consecutive albums."""
        for start in range(0, len(photos), MAX_ALBUM):
            batch = photos[start : start + MAX_ALBUM]
            if len(batch) == 1:  # an album needs two
                await self.send_photo(chat_id, batch[0][0], batch[0][1], caption if start == 0 else "", thread_id)
                continue
            media: list[dict[str, Any]] = [{"type": "photo", "media": f"attach://photo{i}"} for i in range(len(batch))]
            if caption and start == 0:
                media[0]["caption"] = caption[:MAX_CAPTION]
            params: dict[str, Any] = {"chat_id": chat_id, "media": json.dumps(media)}
            if thread_id:
                params["message_thread_id"] = thread_id
            files = {f"photo{i}": (name, content, "image/png") for i, (name, content) in enumerate(batch)}
            await self._call("sendMediaGroup", params, files=files)

    async def download(self, file_id: str) -> bytes:
        """The bytes of a file an admin sent. Telegram serves files of up to 20 MB to bots."""
        info = await self._call("getFile", {"file_id": file_id})
        try:
            response = await self._http.get(self._files + info["file_path"])
        except httpx.HTTPError as exc:
            raise TelegramError("download", None, type(exc).__name__) from None
        if response.status_code != 200:
            raise TelegramError("download", response.status_code, "file not available")
        return response.content

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        """Replace a message's text. Sent without a keyboard, so its buttons go away."""
        await self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:MAX_MESSAGE]})

    async def send_chat_action(self, chat_id: int, thread_id: Optional[int] = None, action: str = "typing") -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "action": action}
        if thread_id:
            params["message_thread_id"] = thread_id
        await self._call("sendChatAction", params)

    async def create_forum_topic(self, chat_id: int, name: str) -> int:
        """Open a topic and return its thread id. Needs the bot to be an admin with `can_manage_topics`."""
        topic = await self._call("createForumTopic", {"chat_id": chat_id, "name": name[:128]})
        return int(topic["message_thread_id"])

    async def close_forum_topic(self, chat_id: int, thread_id: int) -> None:
        await self._call("closeForumTopic", {"chat_id": chat_id, "message_thread_id": thread_id})

    async def forward_message(self, chat_id: int, from_chat_id: int, message_id: int, thread_id: Optional[int] = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "from_chat_id": from_chat_id, "message_id": message_id}
        if thread_id:
            params["message_thread_id"] = thread_id
        await self._call("forwardMessage", params)

    async def set_my_commands(self, commands: list[tuple[str, str]], chat_id: int) -> None:
        """The list a client shows when someone types `/`, for that chat only."""
        params = {
            "commands": [{"command": name, "description": description} for name, description in commands],
            "scope": {"type": "chat", "chat_id": chat_id},
        }
        await self._call("setMyCommands", params)

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def get_chat(self, chat_id: int) -> dict[str, Any]:
        return await self._call("getChat", {"chat_id": chat_id})

    async def get_chat_member(self, chat_id: int, user_id: int) -> dict[str, Any]:
        return await self._call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    async def leave_chat(self, chat_id: int) -> None:
        await self._call("leaveChat", {"chat_id": chat_id})

    async def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        # Telegram refuses a longer one, and the full text is in the rewritten message anyway.
        await self._call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text[:CALLBACK_MAX]})
