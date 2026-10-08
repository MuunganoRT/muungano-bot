"""A door on this machine for the API to say "this just happened".

One request, `POST /hook/<event>` with a small JSON body and the token both
sides already share. It carries an id and nothing else; whatever Duma shows it
reads back from the API. Bound to 127.0.0.1: the API runs next to the bot.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

MAX_BODY = 4096
READ_TIMEOUT_S = 5
Handler = Callable[[str, dict[str, Any]], Awaitable[None]]


async def _read(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
    method, path, _ = (await reader.readline()).decode("latin-1").split(" ", 2)
    headers: dict[str, str] = {}
    while True:
        line = (await reader.readline()).decode("latin-1").strip()
        if not line:
            break
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    if not 0 <= length <= MAX_BODY:
        raise ValueError("body too large")
    return method, path, headers, await reader.readexactly(length)


async def serve(port: int, token: str, handle: Handler) -> asyncio.AbstractServer:
    """Listen for the API's hooks. `handle(event, data)` runs after the API has its answer."""
    tasks: set[asyncio.Task[None]] = set()

    async def safe(event: str, data: dict[str, Any]) -> None:
        try:
            await handle(event, data)
        except Exception as exc:
            log.warning("hook %s failed: %s", event, type(exc).__name__)

    async def on_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status = "400 Bad Request"
        try:
            method, path, headers, body = await asyncio.wait_for(_read(reader), READ_TIMEOUT_S)
            sent = headers.get("x-bot-token", "")
            if not hmac.compare_digest(sent.encode(), token.encode()):
                status = "401 Unauthorized"
            elif method != "POST" or not path.startswith("/hook/") or not path[6:].isidentifier():
                status = "404 Not Found"
            else:
                data = json.loads(body or b"{}")
                if isinstance(data, dict):
                    status = "202 Accepted"
                    task = asyncio.create_task(safe(path[6:], data))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        except Exception as exc:
            log.info("a hook request was not understood: %s", type(exc).__name__)
        try:
            writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(on_connection, "127.0.0.1", port)
