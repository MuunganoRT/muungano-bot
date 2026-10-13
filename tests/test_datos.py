import pytest

from duma.api_client import ALLOWED, ApiError
from duma.pseudonyms import Pseudonyms
from duma.tools import Toolbox

LIST = {
    "success": True,
    "today": "2026-10-08",
    "columns": [
        {"name": "atleta", "kind": "person", "type": "number"},
        {"name": "email", "kind": "variable", "type": "text"},
        {"name": "tarifa", "kind": "open", "type": "number"},
        {"name": "usa_beneficio", "kind": "open", "type": "bool"},
    ],
    "rows": [
        {"atleta": {"id": 10, "name": "Ana Peña"}, "email": "ana@ejemplo.mx", "tarifa": 1200, "usa_beneficio": False},
        {"atleta": {"id": 11, "name": "Beto Ruiz"}, "email": "beto@ejemplo.mx", "tarifa": None, "usa_beneficio": True},
    ],
    "truncated": False,
    "notes": [],
    "matched": {"events": [], "groups": []},
}
TOTALS = {
    "success": True,
    "today": "2026-10-08",
    "columns": [{"name": "membresia", "kind": "open", "type": "text"}, {"name": "debe", "kind": "open", "type": "number"}],
    "rows": [{"membresia": "en_gracia", "debe": 24300.5}],
    "truncated": False,
    "notes": [],
    "matched": {"events": [], "groups": []},
}


class FakeApi:
    def __init__(self, answer=LIST, fail=None):
        self.answer, self.fail, self.calls = answer, fail, []

    async def post(self, path, *, telegram_user_id, json):
        self.calls.append((path, json))
        if self.fail:
            raise self.fail
        return self.answer


async def test_a_list_goes_to_the_chat_and_the_model_sees_no_one():
    api = FakeApi()
    r = await Toolbox(api).run("datos", {"tabla": "atletas", "columnas": ["atleta", "email", "tarifa", "usa_beneficio"]}, 956)
    assert not r.is_error
    for secret in ("Ana", "Peña", "Beto", "Ruiz", "ejemplo.mx"):
        assert secret not in r.to_model
    assert "ATLETA_01 | {email} | 1,200 | no" in r.to_model
    assert "ATLETA_02 | {email} | — | sí" in r.to_model
    assert r.files and r.files[0].photo


async def test_the_csv_carries_the_real_values():
    r = await Toolbox(FakeApi()).run("datos", {"tabla": "atletas", "columnas": ["atleta", "email"], "formato": "csv"}, 956)
    text = r.files[0].content.decode("utf-8-sig")
    assert "Ana Peña" in text and "ana@ejemplo.mx" in text and "1200" in text
    assert "ana@ejemplo.mx" not in r.to_model


async def test_the_codes_are_the_ones_of_the_session():
    names = Pseudonyms()
    assert names.code(11, "Beto Ruiz") == "ATLETA_01"
    r = await Toolbox(FakeApi()).run("datos", {"tabla": "atletas", "columnas": ["atleta"], "salida": "modelo"}, 956, names)
    assert "ATLETA_02 | {email}" in r.to_model and "ATLETA_01 | {email}" in r.to_model
    assert r.to_model.index("ATLETA_02") < r.to_model.index("ATLETA_01")
    assert not r.files and r.direct_text is None


async def test_totals_stay_with_the_model():
    api = FakeApi(TOTALS)
    args = {
        "tabla": "atletas",
        "estado": "activos",
        "agrupar": ["membresia"],
        "medidas": [{"fn": "suma", "de": "pendiente", "como": "debe"}, {"fn": "cuenta"}],
        "filtros": [{"columna": "membresia", "op": "en", "valor": ["en_gracia"]}, {"columna": "tarifa", "op": "vacio"}],
        "personas": [{"tipo": "grupo", "nombre": "Chicago"}],
        "orden": ["-debe"],
        "limite": 20,
    }
    r = await Toolbox(api).run("datos", args, 956)
    path, body = api.calls[0]
    assert path == "/assistant/data"
    assert body == {
        "table": "atletas",
        "member_status": "active",
        "columns": [],
        "filters": [
            {"column": "membresia", "op": "in", "value": ["en_gracia"]},
            {"column": "tarifa", "op": "empty", "value": None},
        ],
        "people": [{"type": "group", "name": "Chicago"}],
        "group_by": ["membresia"],
        "measures": [{"fn": "sum", "of": "pendiente", "as": "debe"}, {"fn": "count"}],
        "formulas": {},
        "order": ["-debe"],
        "limit": 20,
    }
    assert not r.files and r.direct_text is None
    assert "en_gracia | 24,300.50" in r.to_model and "al 2026-10-08" in r.to_model


async def test_no_rows_is_said_plainly():
    r = await Toolbox(FakeApi({**LIST, "rows": []})).run("datos", {"tabla": "atletas", "columnas": ["tarifa"]}, 956)
    assert r.to_model.startswith("Ninguna fila") and not r.files


async def test_what_the_api_refuses_reaches_the_model():
    api = FakeApi(fail=ApiError(400, "'email' cannot be used to filter: it can only be listed"))
    r = await Toolbox(api).run("datos", {"tabla": "atletas", "columnas": ["tarifa"]}, 956)
    assert r.is_error and "cannot be used to filter" in r.to_model


@pytest.mark.parametrize(
    "args",
    [
        {"tabla": "tokens", "columnas": ["id"]},
        {"tabla": "atletas", "columnas": "tarifa"},
        {"tabla": "atletas", "medidas": [{"fn": "mediana", "de": "tarifa"}]},
        {"tabla": "atletas", "columnas": ["tarifa"], "salida": "todos"},
        {"tabla": "atletas", "columnas": ["tarifa"], "limite": 9000},
        {"tabla": "atletas", "columnas": ["tarifa"], "formulas": "tarifa * 2"},
    ],
)
async def test_a_malformed_call_never_reaches_the_api(args):
    api = FakeApi()
    r = await Toolbox(api).run("datos", args, 956)
    assert r.is_error and not api.calls


def test_the_route_is_on_the_list():
    assert any(method == "POST" and rx.match("/assistant/data") for method, rx in ALLOWED)


async def test_workout_columns_carry_their_period_and_their_total():
    answer = {
        **LIST,
        "columns": [{"name": "atleta", "kind": "person", "type": "number"}, {"name": "km", "kind": "open", "type": "number"}],
        "rows": [{"atleta": {"id": 10, "name": "Ana Peña"}, "km": 40.0}],
        "workout_totals": {
            "period": {"from": "2026-09-01", "to": "2026-09-30"},
            "athletes": 1, "entrenos_prescritos": 3, "entrenos_hechos": 2, "km": 40.0, "score": 64.3,
        },
    }
    api = FakeApi(answer)
    args = {"tabla": "atletas", "columnas": ["atleta", "km"], "entrenos": {"desde": "2026-09-01", "hasta": "2026-09-30"}, "salida": "modelo"}
    r = await Toolbox(api).run("datos", args, 956)
    assert api.calls[0][1]["workouts"] == {"from": "2026-09-01", "to": "2026-09-30"}
    assert "ATLETA_01 | 40" in r.to_model
    assert "2 hechos de 3 prescritos, 40 km, score 64.30" in r.to_model


async def test_a_table_about_no_one():
    answer = {
        **LIST,
        "columns": [{"name": "grupo", "kind": "open", "type": "text"}, {"name": "miembros", "kind": "open", "type": "number"}],
        "rows": [{"grupo": "Maratón", "miembros": 12}],
    }
    api = FakeApi(answer)
    r = await Toolbox(api).run("datos", {"tabla": "grupos", "columnas": ["grupo", "miembros"], "salida": "modelo"}, 956)
    assert api.calls[0][1]["table"] == "grupos" and "Maratón | 12" in r.to_model
