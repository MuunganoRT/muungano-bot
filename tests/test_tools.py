from duma.api_client import ApiError
from duma.tools import Toolbox

ANA_P = {"id": 10, "name": "Ana Peña", "group": "Maratón", "active": True}
ANA_R = {"id": 11, "name": "Ana Ruiz", "group": "Fondo 10K", "active": True}

SUMMARY = {
    "success": True,
    "athlete": ANA_P,
    "period": {"from": "2026-09-01", "to": "2026-09-30"},
    "cycle": None,
    "workouts": {"prescribed": 3, "done": 2, "missed": 1},
    "score_avg": 64.3,
    "distance_km": 40.0,
    "avg_pace": "5:25",
    "avg_heart_rate": 161,
    "longest": None,
}


class FakeApi:
    def __init__(self, athletes, summary=SUMMARY, fail=None):
        self.athletes, self.summary, self.fail = athletes, summary, fail
        self.calls = []

    async def get(self, path, *, telegram_user_id, params=None):
        self.calls.append((path, params, telegram_user_id))
        if self.fail:
            raise self.fail
        if path == "/assistant/catalog":
            return {
                "success": True,
                "groups": [{"name": "42k MTY 3:45+", "members": 7}, {"name": "Berlin 4:00hr", "members": 4}],
                "events": [{"name": "Maratón de Chicago", "date": "2026-10-11"}],
            }
        if path == "/assistant/athletes":
            return {"success": True, "athletes": self.athletes, "total": len(self.athletes)}
        if path.endswith("/workouts"):
            return self.workouts
        if path.endswith("/laps"):
            return self.laps
        return self.summary

    async def post(self, path, *, telegram_user_id, json):
        self.calls.append((path, json, telegram_user_id))
        if self.fail:
            raise self.fail
        if path.endswith("/aggregate"):
            return {"success": True, **self.figures, "matched": self.matched, "notes": self.notes}
        if path.endswith("/series"):
            who = {"athlete": {"id": json["athlete_id"], "name": "Ana Peña"}} if "athlete_id" in json else {
                "matched": self.matched, "notes": self.notes
            }
            if json.get("per_athlete"):
                who["people"] = self.people
            return {"success": True, "athletes": 1 if "athlete_id" in json else 12, "weeks": self.weeks, **who}
        if json.get("count_only"):
            return {"success": True, "total": len(self.athletes), "matched": self.matched, "notes": self.notes}
        return {
            "success": True,
            "total": self.total if self.total is not None else len(self.athletes),
            "returned": len(self.athletes),
            "truncated": self.total is not None and self.total > len(self.athletes),
            "athletes": self.athletes,
            "matched": self.matched,
            "notes": self.notes,
        }

    workouts = {
        "success": True,
        "athlete": {"id": 10, "name": "Ana Peña"},
        "period": {"from": "2026-09-01", "to": "2026-09-30"},
        "total": 2,
        "truncated": False,
        "workouts": [
            {"id": 1, "date": "2026-09-10", "type": "Easy Run", "distance_km": 10.0, "duration_sec": 3300,
             "pace": "5:30", "heart_rate": 150.0, "score": 95.0, "laps": 0},
            {"id": 2, "date": "2026-09-20", "type": "Quality Session", "distance_km": 32.1, "duration_sec": 9690,
             "pace": "5:02", "heart_rate": None, "score": 98.0, "laps": 2},
        ],
    }
    laps = {
        "success": True,
        "athlete": {"id": 10, "name": "Ana Peña"},
        "date": "2026-09-20",
        "workout": {"distance_km": 32.1, "duration_sec": 9690.0, "pace": "5:02", "heart_rate": None},
        "total": 2,
        "truncated": False,
        "laps": [
            {"lap": 1, "distance_m": 31000.0, "duration_sec": 9330.0, "pace": "5:01", "heart_rate": 172.0, "score": 98.0},
            {"lap": 2, "distance_m": 1100.0, "duration_sec": 360.0, "pace": "5:27", "heart_rate": None, "score": None},
        ],
    }
    matched = {"events": [], "groups": []}
    notes: list = []
    total = None
    figures = {"athletes": 23}
    weeks = [
        {"week_start": "2026-09-07", "prescribed": 5, "done": 4, "distance_km": 38.2, "score_avg": 71.0,
         "scored": 3, "score_min": 20.0, "score_median": 71.0, "score_max": 96.0},
        {"week_start": "2026-09-14", "prescribed": 0, "done": 0, "distance_km": 0.0, "score_avg": None,
         "scored": 0, "score_min": None, "score_median": None, "score_max": None},
    ]
    people = [
        {"id": 10, "name": "Ana Peña", "prescribed": 5, "done": 4, "distance_km": 38.2, "score_avg": 71.0},
        {"id": 11, "name": "Ana Ruiz", "prescribed": 4, "done": 0, "distance_km": 0.0, "score_avg": 0.0},
        {"id": 12, "name": "Beto Salinas", "prescribed": 0, "done": 0, "distance_km": 0.0, "score_avg": None},
    ]


async def test_search_shows_the_list_and_tells_the_model_nothing_personal():
    box = Toolbox(FakeApi([ANA_P, ANA_R]))
    r = await box.run("buscar_atleta", {"texto": "ana"}, 956)
    assert box._api.calls[-1][1]["member_status"] == "all"  # active and paused unless asked otherwise
    assert "Ana Peña (Maratón)" in r.direct_text and "Ana Ruiz" in r.direct_text
    assert "Ana" not in r.to_model and "Peña" not in r.to_model
    assert not r.is_error


async def test_summary_of_a_unique_match():
    api = FakeApi([ANA_P])
    r = await Toolbox(api).run("resumen_atleta", {"nombre": "ana pena", "desde": "2026-09-01", "hasta": "2026-09-30", "ciclo": False}, 956)
    assert r.direct_text.startswith("Ana Peña\n1 sep – 30 sep 2026\nEntrenos: 2/3 (67%)")
    assert r.to_model == "Resumen enviado al chat: 2 de 3 entrenos."
    path, params, user = api.calls[-1]
    assert path == "/assistant/athletes/10/summary" and params == {"from": "2026-09-01", "to": "2026-09-30"} and user == 956


async def test_the_cycle_flag_is_forwarded():
    api = FakeApi([ANA_P])
    await Toolbox(api).run("resumen_atleta", {"nombre": "ana pena", "ciclo": True}, 1)
    assert api.calls[-1][1] == {"use_cycle": "true"}


async def test_an_ambiguous_name_asks_the_admin_and_not_the_model():
    api = FakeApi([ANA_P, ANA_R])
    r = await Toolbox(api).run("resumen_atleta", {"nombre": "ana"}, 1)
    assert "¿Cuál?" in r.direct_text
    assert "Peña" not in r.to_model and "Ruiz" not in r.to_model
    assert len(api.calls) == 1  # no summary was fetched


async def test_the_group_resolves_a_namesake_without_accents_or_case():
    api = FakeApi([ANA_P, ANA_R])
    r = await Toolbox(api).run("resumen_atleta", {"nombre": "ana", "grupo": "MARATON"}, 1)
    assert r.direct_text.startswith("Ana Peña") and api.calls[-1][0] == "/assistant/athletes/10/summary"


async def test_nobody_found():
    r = await Toolbox(FakeApi([])).run("resumen_atleta", {"nombre": "zzz zzz"}, 1)
    assert r.direct_text == "No encontré a nadie con ese nombre." and not r.is_error


async def test_bad_input_is_an_error_for_the_model_and_nothing_for_the_chat():
    box = Toolbox(FakeApi([ANA_P]))
    for name, args in (
        ("resumen_atleta", {"nombre": "a"}),
        ("resumen_atleta", {"nombre": "ana", "desde": "ayer"}),
        ("resumen_atleta", {}),
        ("buscar_atleta", {"texto": 5}),
        ("borrar_todo", {}),
    ):
        r = await box.run(name, args, 1)
        assert r.is_error and r.direct_text is None, (name, args)


async def test_an_api_error_reaches_the_model_as_an_error_result():
    r = await Toolbox(FakeApi([], fail=ApiError(400, "60 athletes match; narrow the filters"))).run("buscar_atleta", {"texto": "ana"}, 1)
    assert r.is_error and "narrow the filters" in r.to_model and r.direct_text is None


async def test_an_api_failure_is_logged_with_its_real_cause(caplog):
    with caplog.at_level("WARNING", logger="duma.tools"):
        await Toolbox(FakeApi([], fail=ApiError(401, "The API rejected my token"))).run("buscar_atleta", {"texto": "ana"}, 1)
    assert "tool buscar_atleta failed: API status 401: The API rejected my token" in caplog.text


async def test_the_admin_can_narrow_the_search_to_active_or_inactive_only():
    api = FakeApi([ANA_P])
    box = Toolbox(api)
    for estado, expected in (("activos", "active"), ("inactivos", "inactive"), ("todos", "all")):
        await box.run("buscar_atleta", {"texto": "ana", "estado": estado}, 1)
        assert api.calls[-1][1]["member_status"] == expected
    bad = await box.run("buscar_atleta", {"texto": "ana", "estado": "borrados"}, 1)
    assert bad.is_error and "estado" in bad.to_model


async def test_a_summary_looks_for_the_person_among_active_and_paused():
    api = FakeApi([ANA_P])
    await Toolbox(api).run("resumen_atleta", {"nombre": "ana pena"}, 1)
    assert api.calls[0][1]["member_status"] == "all"


async def test_a_filtered_query_translates_the_filters_and_shows_the_list():
    api = FakeApi([{**ANA_P, "events": [{"event": "Maratón de Chicago", "date": "2025-10-12", "time_result": 13510}]}])
    api.matched = {"events": ["Maratón de Chicago (2025-10-12)"], "groups": ["Maratón"]}
    r = await Toolbox(api).run(
        "buscar_atletas",
        {
            "filtros": [
                {"tipo": "evento", "nombre": "chicago", "condicion": "con_tiempo", "anio": 2025},
                {"tipo": "pago", "desde": "2026-10-01", "hasta": "2026-10-06"},
                {"tipo": "grupo", "nombre": "maraton"},
                {"tipo": "entrenos", "desde": "2026-09-01", "hasta": "2026-09-30", "minimo": 10},
            ],
            "estado": "activos",
        },
        956,
    )
    path, body, user = api.calls[-1]
    assert path == "/assistant/athletes/query" and user == 956
    assert body == {
        "filters": [
            {"type": "event", "name": "chicago", "status": "with_time", "year": 2025},
            {"type": "paid", "from": "2026-10-01", "to": "2026-10-06"},
            {"type": "group", "name": "maraton"},
            {"type": "workouts", "from": "2026-09-01", "to": "2026-09-30", "min_done": 10},
        ],
        "member_status": "active",
        "limit": 500,
    }
    assert "Ana Peña (Maratón) · Maratón de Chicago (12 oct 2025) 3:45:10" in r.direct_text
    assert "Ana" not in r.to_model and "Peña" not in r.to_model
    assert "Maratón de Chicago (2025-10-12)" in r.to_model and "1 resultado(s)" in r.to_model


async def test_a_query_without_filters_is_the_whole_roster_active_and_paused():
    api = FakeApi([ANA_P, ANA_R])
    await Toolbox(api).run("buscar_atletas", {}, 1)
    assert api.calls[-1][1] == {"filters": [], "member_status": "all", "limit": 500}


async def test_count_only_shows_nothing_and_gives_the_model_the_number():
    api = FakeApi([ANA_P, ANA_R])
    r = await Toolbox(api).run("buscar_atletas", {"filtros": [{"tipo": "grupo", "nombre": "fondo"}], "solo_contar": True}, 1)
    assert api.calls[-1][1]["count_only"] is True and "limit" not in api.calls[-1][1]
    assert r.direct_text is None and r.to_model.startswith("2 persona(s)")


async def test_the_model_hears_what_did_not_match_and_what_was_left_out():
    api = FakeApi([ANA_P])
    api.total, api.notes = 800, ["No event matches 'bostn' with that status"]
    r = await Toolbox(api).run("buscar_atletas", {"filtros": [{"tipo": "evento", "nombre": "bostn"}]}, 1)
    assert "solo trae 1" in r.to_model and "No event matches 'bostn'" in r.to_model
    assert r.direct_text == "800 personas. El archivo trae las primeras 1; acota los filtros para ver al resto."
    assert r.file is not None


async def test_a_long_list_goes_as_a_csv_file_and_a_short_one_as_text():
    people = [{"id": i, "name": f"Persona {i:02d}", "group": "Maratón", "active": True} for i in range(51)]
    long = await Toolbox(FakeApi(people)).run("buscar_atletas", {}, 1)
    assert long.direct_text == "51 personas. Va la lista completa en el archivo."
    assert long.file.name == "atletas.csv"
    rows = long.file.content.decode("utf-8-sig").splitlines()
    assert rows[0] == "Nombre,Rol,Grupo,Estado" and len(rows) == 52 and rows[1] == "Persona 00,,Maratón,activo"
    assert "Persona" not in long.to_model and "CSV" in long.to_model

    short = await Toolbox(FakeApi(people[:50])).run("buscar_atletas", {}, 1)
    assert short.file is None and short.direct_text.startswith("50 personas:")


async def test_a_malformed_filter_never_reaches_the_api():
    api = FakeApi([ANA_P])
    box = Toolbox(api)
    for filtros in (
        [{"tipo": "sql", "nombre": "x"}],
        [{"tipo": "evento"}],
        [{"tipo": "evento", "nombre": "chicago", "condicion": "ganaron"}],
        [{"tipo": "evento", "nombre": "chicago", "anio": "2025"}],
        [{"tipo": "pago", "desde": "2026-10-01"}],
        [{"tipo": "pago", "desde": "2026-10-06", "hasta": "2026-10-01"}],
        [{"tipo": "entrenos", "desde": "2026-09-01", "hasta": "2026-09-30"}],
        [{"tipo": "entrenos", "desde": "2026-09-01", "hasta": "2026-09-30", "minimo": True}],
        ["chicago"],
        "chicago",
        [{"tipo": "grupo", "nombre": "ab"}] * 9,
    ):
        r = await box.run("buscar_atletas", {"filtros": filtros}, 1)
        assert r.is_error and r.direct_text is None, filtros
    assert api.calls == []


async def test_figures_go_to_the_model_as_numbers_and_nothing_goes_to_the_chat():
    api = FakeApi([])
    api.matched = {"events": [], "groups": ["Maratón"]}
    api.figures = {
        "athletes": 23,
        "period": {"from": "2026-09-01", "to": "2026-09-30"},
        "payments": {"count": 12, "total": 14400.0, "without_amount": 2},
        "workouts": {"prescribed": 300, "done": 250, "missed": 50, "completion_pct": 83},
        "distance_km": 1234.5,
        "score_avg": 78.2,
    }
    r = await Toolbox(api).run(
        "cifras",
        {
            "metricas": ["personas", "pagos", "entrenos", "km", "score", "km"],
            "filtros": [{"tipo": "grupo", "nombre": "maraton"}],
            "desde": "2026-09-01",
            "hasta": "2026-09-30",
        },
        956,
    )
    path, body, user = api.calls[-1]
    assert path == "/assistant/athletes/aggregate" and user == 956
    assert body == {
        "filters": [{"type": "group", "name": "maraton"}],
        "member_status": "all",
        "metrics": ["athletes", "payments", "workouts", "distance_km", "score_avg"],
        "period": {"from": "2026-09-01", "to": "2026-09-30"},
    }
    assert r.direct_text is None and r.file is None and not r.is_error
    assert r.to_model == (
        "Personas: 23. Periodo: 2026-09-01 a 2026-09-30. "
        "Pagos aprobados: 12, suman $14,400.00 MXN (2 sin monto capturado, no suman). "
        "Entrenos: 250 hechos de 300 prescritos (83%). Distancia: 1234.5 km. Score promedio: 78.2%. "
        "Grupos: Maratón."
    )


async def test_a_head_count_needs_no_period_and_every_other_figure_does():
    api = FakeApi([])
    box = Toolbox(api)
    r = await box.run("cifras", {"metricas": ["personas"], "estado": "inactivos"}, 1)
    assert api.calls[-1][1] == {"filters": [], "member_status": "inactive", "metrics": ["athletes"]}
    assert r.to_model == "Personas: 23."

    for args in (
        {"metricas": ["pagos"]},
        {"metricas": ["pagos"], "desde": "2026-10-06", "hasta": "2026-10-01"},
        {"metricas": []},
        {"metricas": ["sueldos"]},
        {"metricas": "personas"},
        {},
    ):
        bad = await box.run("cifras", args, 1)
        assert bad.is_error and bad.direct_text is None, args
    assert len(api.calls) == 1


async def test_a_period_without_prescribed_workouts_has_no_score_and_says_so():
    api = FakeApi([])
    api.figures = {"athletes": 3, "period": {"from": "2026-09-01", "to": "2026-09-30"}, "score_avg": None}
    r = await Toolbox(api).run("cifras", {"metricas": ["score"], "desde": "2026-09-01", "hasta": "2026-09-30"}, 1)
    assert "Score promedio: sin entrenos prescritos." in r.to_model


async def with_preferences(tmp_path):
    from duma.confirmations import Confirmations
    from duma.database import SqliteDatabase
    from duma.preferences import Preferences

    store = await Confirmations.open(SqliteDatabase(tmp_path / "bot.sqlite"), 60)
    prefs = Preferences(tmp_path / "preferencias.md", "America/Monterrey")
    return Toolbox(FakeApi([]), store, prefs), store, prefs


async def test_a_preference_is_proposed_with_buttons_and_not_saved(tmp_path):
    box, store, prefs = await with_preferences(tmp_path)
    r = await box.run("guardar_preferencia", {"regla": "Los reportes de grupo siempre en tabla"}, 956)
    assert r.direct_text == "Regla permanente que pide el admin 956:\n«Los reportes de grupo siempre en tabla»"
    assert [label for label, _ in r.buttons] == ["Guardar", "Cancelar"] and "NO está guardada" in r.to_model
    assert prefs.rules() == []  # nothing is written until the click

    status, pending = await store.claim(r.buttons[0][1].split(":")[1], 956)
    assert status == "ok" and pending.kind == "preference_add" and pending.summary == r.direct_text
    assert pending.payload == {"rule": "Los reportes de grupo siempre en tabla", "replace": []}


async def test_a_bad_or_surplus_preference_is_an_error_and_proposes_nothing(tmp_path):
    from duma.preferences import MAX_RULES

    box, _, prefs = await with_preferences(tmp_path)
    for args in ({"regla": "ok"}, {"regla": 5}, {}):
        r = await box.run("guardar_preferencia", args, 1)
        assert r.is_error and r.buttons is None and r.direct_text is None, args
    for i in range(MAX_RULES):
        prefs.add(1, f"regla número {i}")
    full = await box.run("guardar_preferencia", {"regla": "una más de la cuenta"}, 1)
    assert full.is_error and "tope" in full.to_model


async def test_without_a_place_to_keep_them_the_tool_is_not_offered():
    box = Toolbox(FakeApi([]))
    assert "guardar_preferencia" not in [s["name"] for s in box.schemas]
    assert (await box.run("guardar_preferencia", {"regla": "siempre en tabla"}, 1)).is_error


async def test_a_rule_that_resolves_a_clash_shows_what_it_replaces(tmp_path):
    box, store, prefs = await with_preferences(tmp_path)
    prefs.add(1, "Las listas siempre en tabla")
    prefs.add(1, "Primero el score")
    r = await box.run("guardar_preferencia", {"regla": "Las listas en tabla, salvo si son de menos de 5 personas", "reemplaza": [1, 1]}, 956)
    assert r.direct_text == (
        "Regla permanente que pide el admin 956:\n«Las listas en tabla, salvo si son de menos de 5 personas»\n\n"
        "Reemplaza a:\n«Las listas siempre en tabla»"
    )
    assert "con cuál regla chocaba" in r.to_model
    _, pending = await store.claim(r.buttons[0][1].split(":")[1], 956)
    assert pending.payload["replace"] == [prefs.lines()[0]] and prefs.rules() == ["Las listas siempre en tabla", "Primero el score"]

    for bad in ([3], [0], ["1"], [True], "1"):
        assert (await box.run("guardar_preferencia", {"regla": "otra regla más", "reemplaza": bad}, 1)).is_error, bad


async def test_the_model_is_told_to_check_the_saved_rules_before_proposing(tmp_path):
    box, _, _ = await with_preferences(tmp_path)
    description = next(s for s in box.schemas if s["name"] == "guardar_preferencia")["description"]
    assert "contradice" in description and "reemplaza" in description and "cómo interpretas" in description


async def test_analysis_rows_reach_the_model_with_codes_and_never_a_name():
    from duma.pseudonyms import Pseudonyms

    ana = {**ANA_P, "role": "runner", "events": [{"event": "42k Chicago", "date": "2025-10-12", "time_result": 12960}], "last_payment": {"date": "2026-10-03", "amount": 1200.0}}
    luis = {"id": 12, "name": "Luis Coach", "role": "coach", "group": None, "active": False}
    api, names = FakeApi([ana, luis]), Pseudonyms()
    r = await Toolbox(api).run("consultar", {"filtros": [{"tipo": "evento", "nombre": "chicago"}]}, 956, names)
    assert r.direct_text is None and r.file is None and not r.is_error
    assert r.to_model.splitlines() == [
        "2 persona(s).",
        "ATLETA_01 | runner | grupo Maratón | activo | evento 42k Chicago (2025-10-12) tiempo 3:36:00 | último pago 2026-10-03 $1,200.00",
        "ATLETA_02 | coach | grupo ninguno | inactivo",
    ]
    assert api.calls[-1][1]["limit"] == 60 and len(api.calls) == 1
    assert names.restore("ATLETA_02") == "Luis Coach"


async def test_analysis_with_a_period_adds_each_persons_workouts():
    from duma.pseudonyms import Pseudonyms

    api = FakeApi([ANA_P, ANA_R])
    r = await Toolbox(api).run("consultar", {"desde": "2026-09-01", "hasta": "2026-09-30"}, 1, Pseudonyms())
    rows = r.to_model.splitlines()
    assert rows[0] == "2 persona(s). Periodo de entrenos: 2026-09-01 a 2026-09-30."
    assert rows[1].endswith("entrenos 2/3 | score 64.3% | km 40.0 | ritmo 5:25 min/km | FC 161 lpm")
    summaries = [c for c in api.calls if c[0].endswith("/summary")]
    assert {c[0] for c in summaries} == {"/assistant/athletes/10/summary", "/assistant/athletes/11/summary"}
    assert all(c[1] == {"from": "2026-09-01", "to": "2026-09-30"} for c in summaries) and api.calls[0][1]["limit"] == 25


async def test_too_many_people_to_analyse_is_an_error_and_not_a_partial_list():
    api = FakeApi([ANA_P])
    api.total = 80
    r = await Toolbox(api).run("consultar", {}, 1)
    assert r.is_error and "80 people match" in r.to_model and "ATLETA" not in r.to_model
    for bad in ({"desde": "2026-09-01"}, {"desde": "2026-09-30", "hasta": "2026-09-01"}):
        assert (await Toolbox(api).run("consultar", bad, 1)).is_error


async def test_a_summary_can_be_asked_by_code_without_searching_by_name():
    from duma.pseudonyms import Pseudonyms

    names = Pseudonyms()
    names.code(10, "Ana Peña")
    api = FakeApi([])
    r = await Toolbox(api).run("resumen_atleta", {"nombre": "ATLETA_01"}, 1, names)
    assert [c[0] for c in api.calls] == ["/assistant/athletes/10/summary"] and r.direct_text.startswith("Ana Peña")


PNG = b"\x89PNG\r\n\x1a\n"


async def test_a_chart_of_one_athlete_goes_to_the_chat_as_a_picture_and_the_model_gets_no_figures():
    api = FakeApi([ANA_P])
    r = await Toolbox(api).run(
        "grafica", {"metrica": "entrenos", "nombre": "ana peña", "desde": "2026-09-07", "hasta": "2026-09-20"}, 956
    )
    path, body, user = api.calls[-1]
    assert path == "/assistant/athletes/series" and user == 956
    assert body == {"period": {"from": "2026-09-07", "to": "2026-09-20"}, "athlete_id": 10}
    assert r.file.name == "entrenos.png" and r.file.photo and r.file.content.startswith(PNG)
    assert r.to_model == "Gráfica de entrenos enviada al chat: 2 semanas, del 2026-09-07 al 2026-09-20."
    assert "Ana" not in r.to_model and "38.2" not in r.to_model


async def test_a_chart_of_a_set_tells_the_model_the_weekly_totals():
    api = FakeApi([])
    api.matched = {"events": [], "groups": ["Maratón"]}
    r = await Toolbox(api).run(
        "grafica",
        {"metrica": "km", "filtros": [{"tipo": "grupo", "nombre": "maraton"}], "estado": "activos",
         "desde": "2026-09-07", "hasta": "2026-09-20"},
        1,
    )
    assert api.calls[-1][1] == {
        "period": {"from": "2026-09-07", "to": "2026-09-20"},
        "filters": [{"type": "group", "name": "maraton"}],
        "member_status": "active",
    }
    assert r.file.photo and r.file.content.startswith(PNG)
    assert "2026-09-07: 4/5 entrenos, 38.2 km, score 71.0" in r.to_model
    assert "2026-09-14: 0/0 entrenos, 0.0 km, sin score" in r.to_model and "Grupos: Maratón." in r.to_model


async def test_a_chart_without_dates_covers_the_last_eight_weeks():
    from datetime import date

    api = FakeApi([ANA_P])
    await Toolbox(api).run("grafica", {"metrica": "score", "nombre": "ana"}, 1)
    period = api.calls[-1][1]["period"]
    assert (date.fromisoformat(period["to"]) - date.fromisoformat(period["from"])).days == 55


async def test_a_chart_asks_which_one_when_the_name_is_ambiguous_and_draws_nothing():
    api = FakeApi([ANA_P, ANA_R])
    r = await Toolbox(api).run("grafica", {"metrica": "km", "nombre": "ana"}, 1)
    assert r.file is None and "Ana Ruiz" in r.direct_text and "candidatos" in r.to_model
    assert all(not path.endswith("/series") for path, _, _ in api.calls)


async def test_a_chart_with_nothing_to_draw_says_so_instead_of_an_empty_picture():
    api = FakeApi([ANA_P])
    api.weeks = [{"week_start": "2026-09-07", "prescribed": 0, "done": 0, "distance_km": 0.0, "score_avg": None}]
    r = await Toolbox(api).run("grafica", {"metrica": "entrenos", "nombre": "ana"}, 1)
    assert r.file is None and r.direct_text == "No hay nada que graficar de Ana Peña en ese periodo."


async def test_a_chart_refuses_a_bad_request_before_calling_the_api():
    api = FakeApi([ANA_P])
    box = Toolbox(api)
    for args in (
        {"metrica": "ritmo", "nombre": "ana"},
        {"metrica": "km", "nombre": "ana", "filtros": [{"tipo": "grupo", "nombre": "maraton"}]},
        {"metrica": "km", "nombre": "ana", "desde": "2026-09-30", "hasta": "2026-09-01"},
    ):
        assert (await box.run("grafica", args, 1)).is_error, args
    assert api.calls == []


GROUP = [{"tipo": "grupo", "nombre": "maraton"}]
SEPTEMBER = {"desde": "2026-09-07", "hasta": "2026-09-20"}


async def test_a_ranking_asks_for_the_breakdown_and_keeps_names_and_figures_in_the_picture():
    api = FakeApi([])
    r = await Toolbox(api).run("grafica", {"metrica": "score", "tipo": "ranking", "filtros": GROUP, **SEPTEMBER}, 1)
    assert api.calls[-1][1]["per_athlete"] is True
    assert r.file.name == "ranking_score.png" and r.file.photo and r.file.content.startswith(PNG)
    # Beto had nothing prescribed: he is not ranked last with a zero he did not earn.
    assert "2 personas con entrenos prescritos, se muestran todas" in r.to_model
    assert "Ana" not in r.to_model and "71" not in r.to_model


async def test_a_spread_chart_tells_the_model_the_weekly_range_and_asks_for_no_breakdown():
    api = FakeApi([])
    r = await Toolbox(api).run("grafica", {"metrica": "score", "tipo": "dispersion", "filtros": GROUP, **SEPTEMBER}, 1)
    assert "per_athlete" not in api.calls[-1][1]
    assert r.file.name == "dispersion_score.png" and r.file.content.startswith(PNG)
    assert "2026-09-07: 3 personas, 20.0 / 71.0 / 96.0" in r.to_model and "2026-09-14: nadie" in r.to_model


async def test_a_ranking_of_people_with_nothing_prescribed_draws_nothing():
    api = FakeApi([])
    api.people = [FakeApi.people[2]]
    r = await Toolbox(api).run("grafica", {"metrica": "km", "tipo": "ranking", "filtros": GROUP}, 1)
    assert r.file is None and r.direct_text.startswith("No hay nada que graficar")


async def test_ranking_and_spread_refuse_what_they_cannot_draw_before_calling_the_api():
    api = FakeApi([ANA_P])
    box = Toolbox(api)
    for args in (
        {"metrica": "score", "tipo": "ranking", "nombre": "ana"},
        {"metrica": "km", "tipo": "dispersion", "filtros": GROUP},
        {"metrica": "score", "tipo": "pastel", "filtros": GROUP},
    ):
        assert (await box.run("grafica", args, 1)).is_error, args
    assert api.calls == []


async def test_the_catalogue_goes_to_the_model_only_with_names_and_head_counts():
    api = FakeApi([])
    r = await Toolbox(api).run("catalogo", {}, 956)
    assert api.calls[-1] == ("/assistant/catalog", None, 956)
    assert r.direct_text is None and r.file is None and not r.is_error
    assert r.to_model == (
        "Grupos (miembros): 42k MTY 3:45+ (7); Berlin 4:00hr (4).\nEventos (fecha): Maratón de Chicago (2026-10-11)."
    )


async def test_a_question_goes_to_the_chat_with_one_button_per_option_for_who_asked():
    r = await Toolbox(FakeApi([])).run(
        "preguntar", {"pregunta": "¿Cuál grupo?", "opciones": ["42k MTY, los 5 grupos", " Berlin 4:00hr "]}, 956
    )
    assert r.direct_text == "¿Cuál grupo?" and not r.is_error
    assert r.buttons == [("42k MTY, los 5 grupos", "q:956:0"), ("Berlin 4:00hr", "q:956:1")]
    assert "No hagas nada más" in r.to_model


async def test_a_question_needs_real_options():
    box = Toolbox(FakeApi([]))
    for options in (["solo una"], ["a", "a"], ["a", "x" * 41], ["a", 3], "a,b", [str(i) for i in range(9)]):
        assert (await box.run("preguntar", {"pregunta": "¿Cuál?", "opciones": options}, 1)).is_error, options


async def test_ambiguous_athletes_come_with_a_button_each():
    r = await Toolbox(FakeApi([ANA_P, ANA_R])).run("resumen_atleta", {"nombre": "ana"}, 956)
    assert r.buttons == [("Ana Peña · Maratón", "q:956:0"), ("Ana Ruiz · Fondo 10K", "q:956:1")]
    # Same name and group: a button could not tell them apart.
    twins = await Toolbox(FakeApi([ANA_P, dict(ANA_P, id=99)])).run("resumen_atleta", {"nombre": "ana"}, 956)
    assert twins.buttons is None and "¿Cuál?" in twins.direct_text


def test_parse_choice_takes_only_its_own_buttons():
    from duma.tools import parse_choice

    assert parse_choice("q:956:2") == (956, 2)
    for data in ("ok:abc", "q:956", "q::1", "q:abc:1", "q:956:x", "", None):
        assert parse_choice(data) is None, data


async def test_a_group_filter_can_join_several_groups():
    api = FakeApi([])
    await Toolbox(api).run(
        "cifras", {"metricas": ["personas"], "filtros": [{"tipo": "grupo", "nombre": "42k MTY", "otros": ["Berlin"]}]}, 1
    )
    assert api.calls[-1][1]["filters"] == [{"type": "group", "name": "42k MTY", "also": ["Berlin"]}]
    bad = await Toolbox(api).run("cifras", {"metricas": ["personas"], "filtros": [{"tipo": "grupo", "nombre": "MTY", "otros": ["x"]}]}, 1)
    assert bad.is_error


PNG = b"\x89PNG"
TABLE_NOTE = "La tabla ya salió al chat como imagen: no repitas sus cifras; comenta en dos o tres líneas lo que importa."


async def test_one_athletes_workouts_go_to_the_chat_as_a_picture_and_to_the_model_as_figures():
    api = FakeApi([ANA_P])
    args = {"nombre": "ana pena", "ciclo": True, "km_min": 30, "km_max": 34}
    r = await Toolbox(api).run("entrenos_atleta", args, 956)
    assert r.direct_text == "" and not r.is_error
    assert r.file.photo and r.file.name == "entrenos.png" and r.file.content.startswith(PNG)
    assert r.to_model.splitlines() == [
        "2 entreno(s) hechos del 2026-09-01 al 2026-09-30. El número tras # es el que pide `vueltas_entreno`.",
        "#1 | 2026-09-10 | Easy Run | 10.0 km | 0:55:00 | 5:30 min/km | FC 150 lpm | score 95.0%",
        "#2 | 2026-09-20 | Quality Session | 32.1 km | 2:41:30 | 5:02 min/km | score 98.0% | 2 vueltas",
        TABLE_NOTE,
    ]
    assert "Ana" not in r.to_model
    path, params, user = api.calls[-1]
    assert path == "/assistant/athletes/10/workouts" and user == 956
    assert params == {"min_km": 30.0, "max_km": 34.0, "use_cycle": "true"}


async def test_workouts_say_when_there_are_none_and_when_only_the_longest_came():
    api = FakeApi([ANA_P])
    api.workouts = {**FakeApi.workouts, "total": 0, "workouts": []}
    r = await Toolbox(api).run("entrenos_atleta", {"nombre": "ana pena"}, 1)
    assert r.to_model == "Sin entrenos hechos del 2026-09-01 al 2026-09-30 con esos filtros."

    api.workouts = {**FakeApi.workouts, "total": 80, "truncated": True}
    r = await Toolbox(api).run("entrenos_atleta", {"nombre": "ana pena"}, 1)
    assert r.to_model.startswith("80 entreno(s) hechos del 2026-09-01 al 2026-09-30. Van solo los 2 más largos.")


async def test_workouts_refuse_a_bad_distance_and_ask_the_admin_about_a_namesake():
    api = FakeApi([ANA_P, ANA_R])
    r = await Toolbox(api).run("entrenos_atleta", {"nombre": "ana", "km_min": 34, "km_max": 30}, 1)
    assert r.is_error and api.calls == []
    r = await Toolbox(api).run("entrenos_atleta", {"nombre": "ana", "km_min": "32"}, 1)
    assert r.is_error and api.calls == []

    r = await Toolbox(api).run("entrenos_atleta", {"nombre": "ana"}, 1)
    assert "Ana Ruiz" in r.direct_text and "Ana" not in r.to_model
    assert all(not path.endswith("/workouts") for path, _, _ in api.calls)


async def test_the_laps_of_one_workout():
    api = FakeApi([ANA_P])
    r = await Toolbox(api).run("vueltas_entreno", {"nombre": "ana pena", "entreno": 2}, 956)
    assert r.direct_text == "" and not r.is_error
    assert r.file.photo and r.file.name == "vueltas.png" and r.file.content.startswith(PNG)
    assert r.to_model.splitlines() == [
        "Entreno #2 del 2026-09-20: 2 vuelta(s).",
        "vuelta 1 | 31000 m | 2:35:30 | 5:01 min/km | FC 172 lpm | score 98.0%",
        "vuelta 2 | 1100 m | 0:06:00 | 5:27 min/km",
        TABLE_NOTE,
    ]

    # An API that does not send the name or the totals yet still gets its table.
    api.laps = {k: v for k, v in FakeApi.laps.items() if k not in ("athlete", "workout")}
    r = await Toolbox(api).run("vueltas_entreno", {"nombre": "ana pena", "entreno": 2}, 956)
    assert r.file.content.startswith(PNG)

    many = [{**FakeApi.laps["laps"][0], "lap": n} for n in range(1, 43)]
    api.laps = {**FakeApi.laps, "total": 42, "laps": many}
    r = await Toolbox(api).run("vueltas_entreno", {"nombre": "ana pena", "entreno": 2}, 956)
    assert r.file is None and r.direct_text is None and "demasiadas vueltas" in r.to_model
    api.laps = FakeApi.laps
    assert api.calls[-1][0] == "/assistant/athletes/10/workouts/2/laps"

    api.laps = {**FakeApi.laps, "total": 0, "laps": []}
    r = await Toolbox(api).run("vueltas_entreno", {"nombre": "ana pena", "entreno": 2}, 956)
    assert r.to_model == "El entreno #2 del 2026-09-20 no tiene vueltas registradas."

    r = await Toolbox(api).run("vueltas_entreno", {"nombre": "ana pena"}, 956)
    assert r.is_error
