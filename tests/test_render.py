from duma.render import render_candidates, render_summary

SUMMARY = {
    "athlete": {"id": 10, "name": "Ana Peña", "active": True, "group": "Maratón"},
    "period": {"from": "2026-09-01", "to": "2026-09-30"},
    "cycle": {"event": "Maratón de Chicago", "event_date": "2026-10-11", "weeks_total": 12, "current_week": 9},
    "workouts": {"prescribed": 37, "done": 34, "missed": 3},
    "score_avg": 91.0,
    "distance_km": 412.46,
    "avg_pace": "5:25",
    "avg_heart_rate": 161,
    "longest": {"date": "2026-09-20", "distance_km": 30.0, "pace": "5:23", "heart_rate": 172.0, "score": 98.0, "title": "Largo", "duration_sec": 9690},
}


def test_full_summary():
    assert render_summary(SUMMARY) == (
        "Ana Peña · Maratón de Chicago, semana 9 de 12\n"
        "1 sep – 30 sep 2026\n"
        "Entrenos: 34/37 (92%) · score 91%\n"
        "412.5 km · ritmo promedio 5:25 min/km · FC promedio 161 lpm\n"
        "Más larga (20 sep): 30 km · 5:23 min/km · 172 lpm · score 98%"
    )


def test_without_cycle_or_with_an_old_one():
    no_cycle = {**SUMMARY, "cycle": None}
    assert render_summary(no_cycle).splitlines()[0] == "Ana Peña"
    past = {**SUMMARY, "cycle": {"event": "Maratón de Chicago", "event_date": "2025-10-12", "weeks_total": 12, "current_week": None}}
    assert render_summary(past).splitlines()[0] == "Ana Peña · Maratón de Chicago (12 oct 2025)"


def test_no_prescribed_workouts_says_so_and_stops():
    empty = {**SUMMARY, "workouts": {"prescribed": 0, "done": 0, "missed": 0}, "score_avg": None, "longest": None}
    assert render_summary(empty).splitlines()[-1] == "Sin entrenos prescritos en ese periodo."


def test_missing_figures_are_left_out_not_invented():
    sparse = {**SUMMARY, "avg_pace": None, "avg_heart_rate": None, "longest": None, "score_avg": None}
    text = render_summary(sparse)
    assert "ritmo" not in text and "FC" not in text and "Más larga" not in text and "score" not in text


def test_an_inactive_athlete_is_marked_and_a_period_across_years_shows_both():
    data = {**SUMMARY, "athlete": {**SUMMARY["athlete"], "active": False}, "period": {"from": "2025-12-15", "to": "2026-01-14"}}
    lines = render_summary(data).splitlines()
    assert "(inactivo)" in lines[0]
    assert lines[1] == "15 dic 2025 – 14 ene 2026"


def test_candidates_hide_ids_and_mention_the_overflow():
    people = [
        {"id": 7771, "name": "Ana Peña", "group": "Maratón", "active": True, "role": "runner"},
        {"id": 8882, "name": "Ana Ruiz", "group": None, "active": False, "role": "runner"},
        {"id": 9993, "name": "José Adrián Morales", "group": None, "active": True, "role": "coach"},
    ]
    text = render_candidates(people, 12, question=True)
    assert "7771" not in text and "8882" not in text
    assert "- Ana Peña (Maratón)" in text and "- Ana Ruiz (inactivo)" in text
    assert "- José Adrián Morales (Coach)" in text  # a coach says so; a runner does not
    assert "…y 9 más" in text
    assert render_candidates([], 0, question=False) == "No encontré a nadie con ese nombre."


def test_matches_carry_the_columns_their_filters_were_about():
    from duma.render import render_matches

    found = {
        "total": 3,
        "athletes": [
            {
                "name": "Ana Peña",
                "role": "runner",
                "group": "Maratón",
                "active": True,
                "events": [{"event": "Maratón de Chicago", "date": "2025-10-12", "time_result": None}],
                "last_payment": {"date": "2026-10-03", "amount": 1200.0},
            },
            {"name": "Luis Coach", "role": "coach", "group": None, "active": False, "last_payment": {"date": "2026-10-01", "amount": None}},
        ],
    }
    assert render_matches(found) == (
        "3 personas:\n"
        "- Ana Peña (Maratón) · Maratón de Chicago (12 oct 2025) · pagó $1,200 MXN el 3 oct 2026\n"
        "- Luis Coach (Coach, inactivo) · pagó el 1 oct 2026\n"
        "…y 1 más; acota los filtros."
    )
    assert render_matches({"total": 0, "athletes": []}) == "Nadie cumple esos filtros."


def test_the_csv_adds_only_the_columns_the_filters_brought_and_defuses_formulas():
    from duma.render import matches_csv

    found = {
        "athletes": [
            {
                "name": "Ana Peña",
                "role": "runner",
                "group": "Maratón ",
                "active": True,
                "events": [
                    {"event": "Maratón de Chicago", "date": "2025-10-12", "time_result": 13510},
                    {"event": "21K Monterrey", "date": "2026-03-01", "time_result": None},
                ],
                "last_payment": {"date": "2026-10-03", "amount": 1200.0},
            },
            {"name": "=HYPERLINK(1)", "role": "coach", "group": None, "active": False},
        ],
    }
    content = matches_csv(found)
    assert content.startswith(b"\xef\xbb\xbf")
    assert content.decode("utf-8-sig").splitlines() == [
        "Nombre,Rol,Grupo,Estado,Evento,Fecha del evento,Tiempo,Último pago,Monto",
        "Ana Peña,runner,Maratón,activo,Maratón de Chicago; 21K Monterrey,2025-10-12; 2026-03-01,3:45:10; ,2026-10-03,1200.0",
        "'=HYPERLINK(1),coach,,inactivo,,,,,",
    ]
