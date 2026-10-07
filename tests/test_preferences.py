import pytest

from duma.preferences import MAX_RULES, PreferenceError, Preferences


def make(tmp_path):
    return Preferences(tmp_path / "state" / "preferencias.md", "America/Monterrey")


def test_rules_are_saved_one_per_line_with_date_and_who_asked(tmp_path):
    prefs = make(tmp_path)
    assert prefs.rules() == [] and prefs.for_prompt() == ""
    prefs.add(10, "  Los reportes de grupo\nsiempre en tabla ")
    prefs.add(20, "Primero el score, luego los km")
    assert prefs.rules() == ["Los reportes de grupo siempre en tabla", "Primero el score, luego los km"]
    line = prefs.lines()[0]
    assert line.startswith("- 20") and " · 10 · Los reportes de grupo siempre en tabla" in line
    assert (tmp_path / "state" / "preferencias.md").stat().st_mode & 0o777 == 0o600


def test_the_prompt_gets_the_rules_without_who_asked_and_says_what_they_cannot_do(tmp_path):
    prefs = make(tmp_path)
    prefs.add(123456, "Los reportes de grupo siempre en tabla")
    text = prefs.for_prompt()
    assert text.endswith("1. Los reportes de grupo siempre en tabla") and "123456" not in text
    assert "ganan las anteriores" in text and "cómo interpretas" in text


def test_removing_takes_the_exact_line_and_reports_one_that_is_gone(tmp_path):
    prefs = make(tmp_path)
    prefs.add(10, "regla uno")
    prefs.add(10, "regla dos")
    first = prefs.lines()[0]
    assert prefs.remove(first) and prefs.rules() == ["regla dos"]
    assert not prefs.remove(first)


def test_a_rule_edited_by_hand_is_still_read(tmp_path):
    path = tmp_path / "preferencias.md"
    path.write_text("# notas del equipo\n\n- una regla escrita a mano\n", encoding="utf-8")
    assert Preferences(path, "America/Monterrey").rules() == ["una regla escrita a mano"]


def test_too_short_too_long_and_too_many_are_refused(tmp_path):
    prefs = make(tmp_path)
    for bad in ("ok", "x" * 301):
        with pytest.raises(PreferenceError):
            prefs.add(10, bad)
    for i in range(MAX_RULES):
        prefs.add(10, f"regla número {i}")
    with pytest.raises(PreferenceError):
        prefs.add(10, "una más de la cuenta")


def test_a_rule_can_replace_others_in_one_write_and_frees_their_room(tmp_path):
    prefs = make(tmp_path)
    for i in range(MAX_RULES):
        prefs.add(10, f"regla número {i}")
    first, second = prefs.lines()[:2]
    prefs.add(20, "una que resuelve el choque", replace=(first, second, "- una línea que ya no existe"))
    rules = prefs.rules()
    assert len(rules) == MAX_RULES - 1 and rules[-1] == "una que resuelve el choque" and "regla número 0" not in rules
