import pytest

from duma.charts import METRICS, has_data, has_spread, ranked, ranking_png, spread_png, weekly_png

WEEKS = [
    {"week_start": "2026-09-07", "prescribed": 5, "done": 4, "distance_km": 38.2, "score_avg": 71.0},
    {"week_start": "2026-09-14", "prescribed": 0, "done": 0, "distance_km": 0.0, "score_avg": None},
    {"week_start": "2026-09-21", "prescribed": 4, "done": 0, "distance_km": 0.0, "score_avg": 0.0},
]


@pytest.mark.parametrize("metric", METRICS)
def test_every_metric_draws_a_png_even_with_an_empty_week(metric):
    png = weekly_png(WEEKS, metric, "Ana Peña · 7 sep – 27 sep 2026")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    # Width and height, from the PNG header: the size the constants ask for.
    assert (int.from_bytes(png[16:20]), int.from_bytes(png[20:24])) == (1440, 800)


def test_an_unknown_metric_is_refused():
    with pytest.raises(ValueError):
        weekly_png(WEEKS, "ritmo", "")


def test_has_data_is_about_the_metric_asked():
    missed_only = [WEEKS[2]]
    # Prescribed and not run: there is something to show for workouts and score, and no kilometres.
    assert has_data(missed_only, "entrenos") and has_data(missed_only, "score")
    assert not has_data(missed_only, "km")
    assert not has_data([WEEKS[1]], "entrenos")


def person(name, done, prescribed=10, km=None):
    score = round(9.0 * done, 1) if prescribed else None
    return {"name": name, "prescribed": prescribed, "done": done, "distance_km": km if km is not None else done * 8.0, "score_avg": score}


def test_ranked_is_best_first_without_those_who_had_nothing_prescribed():
    people = [person("Carla", 0), person("Ana", 4), person("Beto", 9), person("Diego", 0, prescribed=0)]
    assert [r["name"] for r in ranked(people, "score")] == ["Beto", "Ana", "Carla"]
    # Completion is done over prescribed, not the count: 3 of 3 beats 4 of 10.
    assert [r["label"] for r in ranked([person("Ana", 4), person("Eva", 3, prescribed=3)], "entrenos")] == ["3/3", "4/10"]
    assert [r["name"] for r in ranked([person("Zoe", 5), person("Ana", 5)], "km")] == ["Ana", "Zoe"]


@pytest.mark.parametrize("metric", METRICS)
def test_a_ranking_draws_whole_or_as_two_ends_and_grows_with_its_rows(metric):
    few = ranking_png([person(f"Atleta {i}", i) for i in range(4)], metric, "4 personas")
    many = ranking_png([person(f"Atleta con nombre muy largo {i}", i % 11) for i in range(30)], metric, "30 personas")
    height = lambda png: int.from_bytes(png[20:24])
    assert few.startswith(b"\x89PNG") and many.startswith(b"\x89PNG")
    # 4 rows against 5 + a gap + 5: the second picture is taller, and no taller than that.
    assert height(few) < height(many) == round((1.7 + 0.42 * 11) * 160)


def test_a_ranking_of_nobody_is_refused():
    with pytest.raises(ValueError):
        ranking_png([person("Diego", 0, prescribed=0)], "score", "")


def test_the_spread_draws_with_empty_weeks_and_knows_when_there_is_nothing():
    weeks = [
        {"week_start": "2026-09-07", "scored": 3, "score_min": 20.0, "score_median": 71.0, "score_max": 96.0},
        {"week_start": "2026-09-14", "scored": 0, "score_min": None, "score_median": None, "score_max": None},
        {"week_start": "2026-09-21", "scored": 1, "score_min": 50.0, "score_median": 50.0, "score_max": 50.0},
    ]
    assert spread_png(weeks, "Maratón").startswith(b"\x89PNG")
    assert has_spread(weeks) and not has_spread([weeks[1]])
