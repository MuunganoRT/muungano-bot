"""Files an admin sends: which ones Duma reads, and how they reach the model.

Images and PDFs go to the model as they are; text files, as text; a voice note
is turned into text first (`voice.py`) and then it is a typed message like any
other. Nothing is written to disk. A file that is too big is refused whole: cutting it would
leave the model reading part of it as if it were all of it.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Optional

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
TEXT_SUFFIXES = (".csv", ".tsv", ".txt", ".md", ".json")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 300 * 1024

NO_VIDEO = "Video todavía no lo entiendo. Mándame texto, una nota de voz, una imagen, un PDF o un CSV."
VOICE_TOO_LONG = "Esa nota de voz es muy larga (tope: {limit} s). Mándame una más corta o escríbemelo."
NO_VOICE_HERE = "En este servidor todavía no puedo escuchar notas de voz. Escríbemelo."
VOICE_NOT_READY = "Todavía estoy descargando el modelo de voz. Dame unos minutos o escríbemelo."
NOT_UNDERSTOOD = "No logré entender el audio. ¿Me lo repites o me lo escribes?"
HEARD = "Entendí: «{text}»"
MAX_VOICE_BYTES = 5 * 1024 * 1024
NO_EXCEL = "Todavía no leo Excel. Expórtalo a CSV y mándamelo."
UNKNOWN_TYPE = "Ese tipo de archivo no lo sé leer. Me sirven imágenes, PDF y archivos de texto o CSV."
TOO_BIG = "Ese archivo pesa demasiado para leerlo completo (tope: {limit}). Mándame uno más chico o solo la parte que importa."
DEFAULT_QUESTION = "Revisa este archivo y dime qué ves."


class Unreadable(Exception):
    """A file Duma will not read, with the line the admin gets."""


@dataclass(frozen=True)
class Attachment:
    kind: str  # image | pdf | text | voice
    file_id: str
    mime: str
    name: str
    size: int
    seconds: int = 0


def _size(limit: int) -> str:
    return f"{limit // (1024 * 1024)} MB" if limit >= 1024 * 1024 else f"{limit // 1024} KB"


def _check(kind: str, size: int) -> None:
    limit = {"image": MAX_IMAGE_BYTES, "pdf": MAX_PDF_BYTES, "text": MAX_TEXT_BYTES, "voice": MAX_VOICE_BYTES}[kind]
    if size > limit:
        raise Unreadable(TOO_BIG.format(limit=_size(limit)))


def pick(message: dict[str, Any], max_voice_s: int = 120) -> Optional[Attachment]:
    """The file in a Telegram message, None when it has none. Raises `Unreadable` for one Duma does not take."""
    if any(message.get(field) for field in ("video", "video_note")):
        raise Unreadable(NO_VIDEO)

    spoken = message.get("voice") or message.get("audio")
    photo = message.get("photo")
    if spoken:
        seconds = int(spoken.get("duration") or 0)
        if seconds > max_voice_s:
            raise Unreadable(VOICE_TOO_LONG.format(limit=max_voice_s))
        mime = spoken.get("mime_type") or "audio/ogg"
        attachment = Attachment("voice", spoken["file_id"], mime, "nota de voz", spoken.get("file_size") or 0, seconds)
    elif photo:
        largest = max(photo, key=lambda size: size.get("file_size") or 0)
        attachment = Attachment("image", largest["file_id"], "image/jpeg", "foto.jpg", largest.get("file_size") or 0)
    elif message.get("document"):
        document = message["document"]
        mime = (document.get("mime_type") or "").lower()
        name = document.get("file_name") or "archivo"
        if mime in IMAGE_TYPES:
            kind = "image"
        elif mime == "application/pdf" or name.lower().endswith(".pdf"):
            kind, mime = "pdf", "application/pdf"
        elif name.lower().endswith((".xlsx", ".xls")) or "spreadsheet" in mime or "excel" in mime:
            raise Unreadable(NO_EXCEL)
        elif mime.startswith("text/") or name.lower().endswith(TEXT_SUFFIXES):
            kind = "text"
        else:
            raise Unreadable(UNKNOWN_TYPE)
        attachment = Attachment(kind, document["file_id"], mime, name, document.get("file_size") or 0)
    else:
        return None

    _check(attachment.kind, attachment.size)
    return attachment


def to_block(attachment: Attachment, data: bytes) -> dict[str, Any]:
    """The content block the model reads. The size is checked again: Telegram does not always report it."""
    _check(attachment.kind, len(data))
    if attachment.kind == "text":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        return {"type": "text", "text": f"Archivo «{attachment.name}»:\n\n{text}"}
    encoded = base64.standard_b64encode(data).decode("ascii")
    if attachment.kind == "pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": encoded}}
    return {"type": "image", "source": {"type": "base64", "media_type": attachment.mime, "data": encoded}}
