import base64

import pytest

from duma import media


def test_the_largest_photo_is_the_one_read():
    picked = media.pick({"photo": [{"file_id": "a", "file_size": 10}, {"file_id": "b", "file_size": 900}]})
    assert (picked.kind, picked.file_id, picked.mime) == ("image", "b", "image/jpeg")


def test_documents_are_told_apart_by_type_and_by_name():
    assert media.pick({"document": {"file_id": "x", "mime_type": "image/png", "file_name": "c.png"}}).kind == "image"
    assert media.pick({"document": {"file_id": "x", "file_name": "reporte.PDF"}}).kind == "pdf"
    assert media.pick({"document": {"file_id": "x", "mime_type": "application/octet-stream", "file_name": "lista.csv"}}).kind == "text"
    assert media.pick({"text": "hola"}) is None


def test_what_cannot_be_read_says_why():
    for message, line in (
        ({"video_note": {"file_id": "v"}}, media.NO_VIDEO),
        ({"voice": {"file_id": "v", "duration": 121}}, media.VOICE_TOO_LONG.format(limit=120)),
        ({"document": {"file_id": "x", "file_name": "a.xlsx"}}, media.NO_EXCEL),
        ({"document": {"file_id": "x", "file_name": "a.zip", "mime_type": "application/zip"}}, media.UNKNOWN_TYPE),
    ):
        with pytest.raises(media.Unreadable, match=line[:20]):
            media.pick(message)


def test_a_file_over_its_limit_is_refused_whole_before_and_after_the_download():
    with pytest.raises(media.Unreadable, match="300 KB"):
        media.pick({"document": {"file_id": "x", "file_name": "a.csv", "file_size": media.MAX_TEXT_BYTES + 1}})
    unreported = media.pick({"document": {"file_id": "x", "file_name": "a.csv"}})  # Telegram gave no size
    with pytest.raises(media.Unreadable):
        media.to_block(unreported, b"x" * (media.MAX_TEXT_BYTES + 1))


def test_blocks_for_text_pdf_and_image():
    text = media.to_block(media.Attachment("text", "x", "text/csv", "a.csv", 0), "Año,Ñu\n".encode("latin-1"))
    assert text == {"type": "text", "text": "Archivo «a.csv»:\n\nAño,Ñu\n"}
    pdf = media.to_block(media.Attachment("pdf", "x", "application/pdf", "a.pdf", 0), b"%PDF-1.4")
    assert pdf["type"] == "document" and base64.standard_b64decode(pdf["source"]["data"]) == b"%PDF-1.4"
    image = media.to_block(media.Attachment("image", "x", "image/webp", "a.webp", 0), b"RIFF")
    assert image["type"] == "image" and image["source"]["media_type"] == "image/webp"


def test_a_voice_note_is_picked_with_its_length():
    note = media.pick({"voice": {"file_id": "v", "duration": 17, "mime_type": "audio/ogg", "file_size": 48738}})
    assert (note.kind, note.file_id, note.seconds, note.size) == ("voice", "v", 17, 48738)
    assert media.pick({"audio": {"file_id": "a", "duration": 30}}).kind == "voice"
    assert media.pick({"voice": {"file_id": "v", "duration": 200}}, max_voice_s=300).seconds == 200
