import httpx
import pytest

from duma.api_client import ApiError, MuunganoApi

TOKEN = "t" * 40


def make(handler, **kwargs):
    client = httpx.AsyncClient(base_url="http://api.test", transport=httpx.MockTransport(handler))
    return MuunganoApi("http://api.test", TOKEN, client=client, **kwargs)


def ok(request):
    return httpx.Response(200, json={"success": True, "athletes": []})


async def test_sends_the_service_token_and_who_asked():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        seen["query"] = dict(request.url.params)
        return ok(request)

    await make(handler).get("/assistant/athletes", telegram_user_id=956, params={"q": "ana"})
    assert seen["x-bot-token"] == TOKEN and seen["x-telegram-user-id"] == "956"
    assert seen["query"] == {"q": "ana"}


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/v2/users"),
        ("GET", "/assistant"),
        ("GET", "/assistant/athletes/abc/summary"),
        ("GET", "/assistant/athletes/1/summary/extra"),
        ("GET", "/assistant/athletes/1/workouts/abc/laps"),
        ("GET", "/assistant/athletes/1/workouts/2"),
        ("POST", "/assistant/athletes/1/workouts"),
        ("DELETE", "/assistant/athletes"),
        ("POST", "/assistant/athletes"),
        ("GET", "/assistant/athletes/query"),
        ("PUT", "/assistant/athletes/query"),
        ("GET", "//assistant/athletes"),
    ],
)
async def test_anything_off_the_allow_list_never_leaves(method, path):
    called = []

    def handler(request):
        called.append(request)
        return ok(request)

    with pytest.raises(ApiError, match="not allowed"):
        await make(handler).request(method, path, telegram_user_id=1)
    assert called == []


async def test_the_read_routes_are_allowed():
    api = make(ok)
    await api.get("/assistant/athletes", telegram_user_id=1)
    await api.get("/assistant/athletes/12/summary", telegram_user_id=1)
    await api.get("/assistant/athletes/12/workouts", telegram_user_id=1)
    await api.get("/assistant/athletes/12/workouts/345/laps", telegram_user_id=1)
    await api.get("/assistant/catalog", telegram_user_id=1)
    for tail in ("athletes/12/plan", "athletes/12/payments", "athletes/12/profile", "receipts"):
        await api.get(f"/assistant/{tail}", telegram_user_id=1)
    with pytest.raises(ApiError, match="not allowed"):
        await api.get("/assistant/athletes/12/receipts/3/archivo", telegram_user_id=1)
    await api.post("/assistant/athletes/query", telegram_user_id=1, json={"filters": []})
    await api.post("/assistant/athletes/aggregate", telegram_user_id=1, json={})
    await api.post("/assistant/athletes/series", telegram_user_id=1, json={})


async def test_the_per_minute_cap():
    api = make(ok, max_per_min=2)
    await api.get("/assistant/athletes", telegram_user_id=1)
    await api.get("/assistant/athletes", telegram_user_id=1)
    with pytest.raises(ApiError) as excinfo:
        await api.get("/assistant/athletes", telegram_user_id=1)
    assert excinfo.value.status == 429


async def test_401_is_reported_as_a_configuration_problem_without_echoing_anything():
    def handler(request):
        return httpx.Response(401, json={"success": False, "error": {"message": "Unauthorized", "code": 401}})

    with pytest.raises(ApiError) as excinfo:
        await make(handler).get("/assistant/athletes", telegram_user_id=1)
    assert excinfo.value.status == 401 and TOKEN not in excinfo.value.message


async def test_an_api_error_envelope_becomes_its_message():
    def handler(request):
        return httpx.Response(400, json={"success": False, "error": {"message": "60 athletes match; narrow the filters", "code": 400}})

    with pytest.raises(ApiError, match="narrow the filters"):
        await make(handler).get("/assistant/athletes", telegram_user_id=1)


async def test_timeouts_connection_errors_and_garbage():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    def garbage(request):
        return httpx.Response(200, text="not json")

    for handler, text in ((timeout, "too long"), (refused, "reach"), (garbage, "unexpected")):
        with pytest.raises(ApiError, match=text):
            await make(handler).get("/assistant/athletes", telegram_user_id=1)


async def test_a_write_route_is_reachable_only_through_write_and_a_file_only_through_get_file():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/file"):
            return httpx.Response(200, content=b"\xff\xd8", headers={"content-type": "image/jpeg"})
        return httpx.Response(200, json={"success": True, "data": {"cubierto_hasta": "2026-12-31"}})

    api = make(handler)
    body = {"accion": "aprobar", "meses": 3}
    with pytest.raises(ApiError, match="not allowed"):
        await api.post("/assistant/receipts/12/decide", telegram_user_id=1, json=body)
    with pytest.raises(ApiError, match="not allowed"):
        await api.get("/assistant/receipts/12/file", telegram_user_id=1)
    with pytest.raises(ApiError, match="not allowed"):
        await api.write("/assistant/athletes/query", telegram_user_id=1, json={})
    assert seen == []

    with pytest.raises(ApiError, match="not allowed"):
        await api.post("/assistant/messages", telegram_user_id=1, json={})
    with pytest.raises(ApiError, match="not allowed"):
        await api.write("/assistant/messages/preview", telegram_user_id=1, json={})
    assert seen == []
    await api.post("/assistant/messages/preview", telegram_user_id=1, json={"everyone": True})
    await api.write("/assistant/messages", telegram_user_id=1, json={})
    with pytest.raises(ApiError, match="not allowed"):
        await api.post("/assistant/applications/3/decide", telegram_user_id=1, json={})
    await api.write("/assistant/applications/3/decide", telegram_user_id=1, json={"action": "wait"})
    await api.get("/assistant/applications", telegram_user_id=1)
    await api.get("/assistant/applications/3", telegram_user_id=1)
    await api.get("/assistant/garmin/errors", telegram_user_id=1)
    await api.get("/assistant/messages", telegram_user_id=1)
    seen.clear()

    assert (await api.write("/assistant/receipts/12/decide", telegram_user_id=1, json=body))["data"]
    assert await api.get_file("/assistant/receipts/12/file", telegram_user_id=1) == (b"\xff\xd8", "image/jpeg")
    await api.get("/assistant/receipts/12", telegram_user_id=1)
    assert [m for m, _ in seen] == ["POST", "GET", "GET"]
