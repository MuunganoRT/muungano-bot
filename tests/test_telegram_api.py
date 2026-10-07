import json
import logging

import httpx
import pytest

from duma.main import setup_logging
from duma.telegram_api import MAX_MESSAGE, Telegram, TelegramError, split_message

TOKEN = "123456:FAKE-TOKEN-FOR-TESTS"


def make(handler):
    return Telegram(TOKEN, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_split_keeps_short_text_whole_and_drops_empty():
    assert split_message("hola") == ["hola"]
    assert split_message("   \n") == []


def test_split_cuts_at_a_line_break_and_loses_nothing():
    text = "\n".join(f"linea {i} " + "x" * 90 for i in range(200))
    pieces = split_message(text)
    assert len(pieces) > 1
    assert all(len(p) <= MAX_MESSAGE for p in pieces)
    assert "".join(p.replace("\n", "") for p in pieces) == text.replace("\n", "")


def test_split_cuts_hard_when_there_is_no_line_break():
    pieces = split_message("y" * 9000)
    assert [len(p) for p in pieces] == [MAX_MESSAGE, MAX_MESSAGE, 1000]


async def test_send_message_sends_each_piece_in_the_topic():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    tg = make(handler)
    await tg.send_message(-1, "z" * 5000, thread_id=7)
    assert len(sent) == 2 and all(p["message_thread_id"] == 7 and p["chat_id"] == -1 for p in sent)


async def test_get_updates_asks_for_the_right_kinds_and_outlasts_the_poll():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["timeout"] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 5}]})

    updates = await make(handler).get_updates(offset=6, timeout=30)
    assert updates == [{"update_id": 5}]
    assert seen["body"]["offset"] == 6
    assert set(seen["body"]["allowed_updates"]) == {"message", "callback_query", "my_chat_member"}
    assert seen["timeout"] == 40


async def test_a_telegram_error_never_carries_the_token():
    def handler(request):
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    with pytest.raises(TelegramError) as excinfo:
        await make(handler).send_message(-1, "hola")
    assert "Unauthorized" in str(excinfo.value)
    assert TOKEN not in str(excinfo.value) and "bot123456" not in str(excinfo.value)


async def test_a_network_error_never_carries_the_token():
    def handler(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    with pytest.raises(TelegramError) as excinfo:
        await make(handler).send_message(-1, "hola")
    assert str(excinfo.value) == "Telegram sendMessage: None ConnectError"


async def test_a_non_json_answer_is_an_error():
    def handler(request):
        return httpx.Response(502, text="<html>bad gateway</html>")

    with pytest.raises(TelegramError, match="not JSON"):
        await make(handler).leave_chat(-1)


def test_setup_logging_silences_the_request_log_that_would_print_the_token():
    setup_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpx2").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


async def test_send_document_uploads_the_file_with_its_caption_in_the_topic():
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers["content-type"], request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    await make(handler).send_document(-100, "atletas.csv", b"Nombre\nAna", "51 personas.", thread_id=7)
    path, content_type, body = seen[0]
    assert path.endswith("/sendDocument") and content_type.startswith("multipart/form-data")
    assert b'filename="atletas.csv"' in body and b"Nombre\nAna" in body
    assert b"51 personas." in body and b'name="message_thread_id"' in body


async def test_a_caption_too_long_for_telegram_goes_first_as_a_message():
    methods = []

    def handler(request):
        methods.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"ok": True, "result": {}})

    await make(handler).send_document(-100, "a.csv", b"x", "y" * 1500)
    assert methods == ["sendMessage", "sendDocument"]


async def test_set_my_commands_is_scoped_to_one_chat():
    sent = []

    def handler(request):
        sent.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": True})

    await make(handler).set_my_commands([("ruun", "Pregunta nueva")], -100)
    assert sent == [("setMyCommands", {"commands": [{"command": "ruun", "description": "Pregunta nueva"}], "scope": {"type": "chat", "chat_id": -100}})]


async def test_edit_message_text_sends_no_keyboard_so_the_buttons_go_away():
    sent = []

    def handler(request):
        sent.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {}})

    await make(handler).edit_message_text(-100, 900, "Regla\n\nGuardada.")
    assert sent == [("editMessageText", {"chat_id": -100, "message_id": 900, "text": "Regla\n\nGuardada."})]


async def test_download_asks_for_the_path_and_then_fetches_the_bytes():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/file_7.csv"}})
        return httpx.Response(200, content=b"Nombre\nAna")

    assert await make(handler).download("abc") == b"Nombre\nAna"
    assert seen[1] == f"/file/bot{TOKEN}/documents/file_7.csv"


async def test_a_failed_download_never_carries_the_url():
    def handler(request):
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/file_7.csv"}})
        return httpx.Response(404)

    with pytest.raises(TelegramError) as caught:
        await make(handler).download("abc")
    assert TOKEN not in str(caught.value) and "404" in str(caught.value)
