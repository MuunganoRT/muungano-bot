import pytest

from duma.charts import METRICS, has_data, weekly_png

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
