"""Charts, drawn here as a PNG held in memory.

The look lives in the constants below: change them and every chart follows.
Nothing is written to disk, and no data leaves the machine to be drawn.
"""

from __future__ import annotations

import io
from typing import Any

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from duma.render import _day, _num

# The console's brand scale (`muungano-web/app/src/ui/ui.css`, `--color-brand-*`), as hex.
BRAND = "#c26e17"
BRAND_SOFT = "#eed6c3"
# The bottom of a ranking: the ones a coach has to look for.
LOW = "#a1a1aa"
INK = "#27272a"
MUTED = "#71717a"
GRID = "#e4e4e7"
BACKGROUND = "#ffffff"

FONT = "DejaVu Sans"
SIZE_IN = (9.0, 5.0)
# 9 in at 160 dpi is 1440 px wide: sharp on a phone, and well under what Telegram accepts for a photo.
DPI = 160

METRICS = ("entrenos", "km", "score")
TITLES = {"entrenos": "Entrenos por semana", "km": "Kilómetros por semana", "score": "Score por semana"}
RANKING_TITLES = {"entrenos": "Ranking de cumplimiento", "km": "Ranking de kilómetros", "score": "Ranking de score"}
# A ranking shows this many from each end; a set no bigger than both ends together is shown whole.
RANKING_ENDS = 5
NAME_MAX = 26


def has_data(weeks: list[dict[str, Any]], metric: str) -> bool:
    if metric == "km":
        return any(w["distance_km"] for w in weeks)
    return any(w["prescribed"] for w in weeks)


def _figure(size: tuple[float, float]) -> tuple[Figure, Any]:
    # A Figure of its own, not pyplot: pyplot keeps global state and this runs off the event loop.
    figure = Figure(figsize=size, dpi=DPI, facecolor=BACKGROUND)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot()
    axes.set_facecolor(BACKGROUND)
    return figure, axes


def _png(figure: Figure) -> bytes:
    out = io.BytesIO()
    figure.savefig(out, format="png")
    return out.getvalue()


def ranked(people: list[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    """Who had something prescribed, best first, each with `value` and the `label` drawn next to the bar."""
    rows = []
    for person in people:
        if not person["prescribed"]:
            continue
        if metric == "km":
            value, label = person["distance_km"], f"{_num(person['distance_km'])} km"
        elif metric == "entrenos":
            value = 100 * person["done"] / person["prescribed"]
            label = f"{person['done']}/{person['prescribed']}"
        else:
            value, label = person["score_avg"], _num(person["score_avg"])
        rows.append({"name": person["name"], "value": value, "label": label})
    # Ties in name order, so the same question draws the same chart twice.
    return sorted(rows, key=lambda r: (-r["value"], r["name"]))


def ranking_png(people: list[dict[str, Any]], metric: str, subtitle: str) -> bytes:
    """Horizontal bars, best on top: everyone, or the first and the last `RANKING_ENDS` of a longer set."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    rows = ranked(people, metric)
    if not rows:
        raise ValueError("nobody to rank")
    if len(rows) > 2 * RANKING_ENDS:
        top, bottom = rows[:RANKING_ENDS], rows[-RANKING_ENDS:]
        first_low = len(rows) - RANKING_ENDS + 1
    else:
        top, bottom, first_low = rows, [], 0
    # y grows upwards, so the best goes at the highest position; one empty slot separates the two ends.
    gap = 1 if bottom else 0
    placed = [(len(top) + len(bottom) + gap - 1 - i, row, BRAND, i + 1) for i, row in enumerate(top)]
    placed += [(len(bottom) - 1 - i, row, LOW, first_low + i) for i, row in enumerate(bottom)]

    slots = len(placed) + gap
    figure, axes = _figure((SIZE_IN[0], 1.7 + 0.42 * slots))
    longest = max(row["value"] for row in rows) or 1
    for y, row, color, position in placed:
        axes.barh(y, row["value"], color=color, height=0.68)
        axes.annotate(row["label"], (row["value"], y), xytext=(6, 0), textcoords="offset points", va="center", **_text())
    axes.set_yticks([y for y, *_ in placed], [f"{position}. {_short(row['name'])}" for _, row, _, position in placed])
    axes.set_xlim(0, (100 if metric != "km" else longest) * 1.12)
    axes.set_ylim(-0.7, slots - 0.3)
    axes.tick_params(axis="y", colors=INK, length=0, labelsize=10, labelfontfamily=FONT)
    axes.tick_params(axis="x", colors=MUTED, length=0, labelsize=10, labelfontfamily=FONT)
    axes.grid(axis="x", color=GRID, linewidth=1)
    axes.set_axisbelow(True)
    for side in ("top", "right", "bottom", "left"):
        axes.spines[side].set_visible(False)

    height = figure.get_figheight()
    figure.text(0.04, 1 - 0.38 / height, RANKING_TITLES[metric], color=INK, fontsize=17, fontweight="bold", fontfamily=FONT)
    figure.text(0.04, 1 - 0.72 / height, subtitle, color=MUTED, fontsize=11, fontfamily=FONT)
    figure.subplots_adjust(left=0.30, right=0.96, top=1 - 1.0 / height, bottom=0.45 / height)
    return _png(figure)


def _short(name: str) -> str:
    return name if len(name) <= NAME_MAX else name[: NAME_MAX - 1].rstrip() + "…"


def has_spread(weeks: list[dict[str, Any]]) -> bool:
    return any(w.get("scored") for w in weeks)


def spread_png(weeks: list[dict[str, Any]], subtitle: str) -> bytes:
    """Per week, the lowest and the highest score among the athletes, and the median between them."""
    figure, axes = _figure(SIZE_IN)
    xs = list(range(len(weeks)))
    low = [w["score_min"] or 0 for w in weeks]
    span = [(w["score_max"] or 0) - (w["score_min"] or 0) for w in weeks]
    # A week with nobody scored has no median: None leaves a gap in the line instead of a zero.
    middle = [float("nan") if w["score_median"] is None else w["score_median"] for w in weeks]

    # One floating bar per week, not a band: a band would draw a spread across the weeks that have none.
    axes.bar(xs, span, bottom=low, color=BRAND_SOFT, width=0.5, label="Del más bajo al más alto")
    axes.plot(xs, middle, color=BRAND, linewidth=2.5, marker="o", markersize=7, label="Mediana")
    for x, w in zip(xs, weeks):
        if w["score_median"] is not None:
            axes.annotate(
                _num(w["score_median"]), (x, w["score_median"]), xytext=(0, 9), textcoords="offset points", ha="center", **_text()
            )
    axes.set_ylim(0, 118)
    axes.set_yticks([0, 25, 50, 75, 100])
    axes.legend(loc="upper left", frameon=False, ncols=2, prop={"family": FONT, "size": 10}, labelcolor=MUTED)
    _weekly_frame(figure, axes, weeks, "Score por semana: qué tan parejo va el grupo", subtitle)
    return _png(figure)


def _weekly_frame(figure: Figure, axes: Any, weeks: list[dict[str, Any]], title: str, subtitle: str) -> None:
    xs = list(range(len(weeks)))
    axes.set_xticks(xs, [_day(w["week_start"]) for w in weeks])
    axes.set_xlim(-0.6, len(weeks) - 0.4)
    axes.tick_params(colors=MUTED, length=0, labelsize=10, labelfontfamily=FONT)
    axes.grid(axis="y", color=GRID, linewidth=1)
    axes.set_axisbelow(True)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(GRID)
    axes.set_xlabel("Semana del", color=MUTED, fontsize=10, fontfamily=FONT, labelpad=8)

    figure.text(0.06, 0.93, title, color=INK, fontsize=17, fontweight="bold", fontfamily=FONT)
    figure.text(0.06, 0.875, subtitle, color=MUTED, fontsize=11, fontfamily=FONT)
    figure.subplots_adjust(left=0.07, right=0.97, top=0.82, bottom=0.14)


def weekly_png(weeks: list[dict[str, Any]], metric: str, subtitle: str) -> bytes:
    """One chart of `metric` over the weeks the API returned, oldest first."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")

    figure, axes = _figure(SIZE_IN)
    xs = list(range(len(weeks)))

    if metric == "entrenos":
        prescribed = [w["prescribed"] for w in weeks]
        done = [w["done"] for w in weeks]
        axes.bar(xs, prescribed, color=BRAND_SOFT, width=0.7, label="Prescritos")
        axes.bar(xs, done, color=BRAND, width=0.7, label="Hechos")
        for x, d, p in zip(xs, done, prescribed):
            if p:
                axes.annotate(f"{d}/{p}", (x, p), xytext=(0, 4), textcoords="offset points", ha="center", **_text())
        axes.set_ylim(0, max(prescribed + [1]) * 1.18)
        axes.yaxis.get_major_locator().set_params(integer=True)
        axes.legend(loc="upper left", frameon=False, ncols=2, prop={"family": FONT, "size": 10}, labelcolor=MUTED)
    elif metric == "km":
        km = [w["distance_km"] for w in weeks]
        axes.bar(xs, km, color=BRAND, width=0.7)
        for x, value in zip(xs, km):
            if value:
                axes.annotate(_num(value), (x, value), xytext=(0, 4), textcoords="offset points", ha="center", **_text())
        axes.set_ylim(0, max(km + [1]) * 1.15)
    else:
        # A week with nothing prescribed has no score: None leaves a gap in the line instead of a zero.
        scores = [float("nan") if w["score_avg"] is None else w["score_avg"] for w in weeks]
        axes.plot(xs, scores, color=BRAND, linewidth=2.5, marker="o", markersize=7)
        for x, w in zip(xs, weeks):
            if w["score_avg"] is not None:
                axes.annotate(
                    _num(w["score_avg"]), (x, w["score_avg"]), xytext=(0, 9), textcoords="offset points", ha="center", **_text()
                )
        axes.set_ylim(0, 110)
        axes.set_yticks([0, 25, 50, 75, 100])

    _weekly_frame(figure, axes, weeks, TITLES[metric], subtitle)
    return _png(figure)


def _text() -> dict[str, Any]:
    return {"color": INK, "fontsize": 10, "fontfamily": FONT}
