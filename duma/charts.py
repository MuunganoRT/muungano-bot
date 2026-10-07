"""Charts, drawn here as a PNG held in memory.

The look lives in the constants below: change them and every chart follows.
Nothing is written to disk, and no data leaves the machine to be drawn.
"""

from __future__ import annotations

import io
import math
import random
from typing import Any

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Polygon

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


# ── Tables ─────────────────────────────────────────────────────────────
#
# The app icon's look: black, its dark spots in the top right corner, white text.

TABLE_BACKGROUND = "#000000"
TABLE_SPOT = "#2e2e2e"
TABLE_INK = "#ffffff"
TABLE_MUTED = "#a1a1aa"
TABLE_RULE = "#3f3f46"
TABLE_BRAND = "#f08a1c"
TABLE_FOOTER = "Duma · Muungano Running Team"
TABLE_WIDTH_IN = 9.0
TABLE_ROW_IN = 0.46
# Past this the picture is too tall to read on a phone without scrolling it like a file.
TABLE_MAX_ROWS = 40
EMPTY_CELL = "—"


def _spot(cx: float, cy: float, radius: float, angle: float, rng: random.Random, aspect: float) -> Polygon:
    """A bean-like blob: a circle bent by two low harmonics, stretched and turned."""
    second, third = rng.uniform(0, 2 * math.pi), rng.uniform(0, 2 * math.pi)
    points = []
    for step in range(72):
        t = 2 * math.pi * step / 72
        r = radius * (1 + 0.16 * math.sin(2 * t + second) + 0.09 * math.sin(3 * t + third))
        x, y = r * 1.55 * math.cos(t), r * math.sin(t)
        points.append(
            (cx + (x * math.cos(angle) - y * math.sin(angle)) / aspect, cy + x * math.sin(angle) + y * math.cos(angle))
        )
    return Polygon(points, closed=True, facecolor=TABLE_SPOT, edgecolor="none", zorder=0)


def _spots(axes: Any, height: float) -> None:
    """The same spots on every table: a fixed seed, placed in inches from the corner and kept apart."""
    rng = random.Random(11)
    placed: list[tuple[float, float, float]] = []
    for _ in range(4000):
        if len(placed) == 13:
            break
        far, turn = rng.uniform(0.0, 3.4), rng.uniform(math.pi, 1.5 * math.pi)
        x, y = TABLE_WIDTH_IN + 0.15 + far * math.cos(turn), height + 0.15 + far * math.sin(turn)
        radius = rng.uniform(0.13, 0.30) * (1.05 - far / 6.5)
        if any(math.hypot(x - a, y - b) < 1.7 * (radius + other) + 0.10 for a, b, other in placed):
            continue
        placed.append((x, y, radius))
        axes.add_patch(_spot(x / TABLE_WIDTH_IN, y / height, radius / height, rng.uniform(0.6, 1.1), rng, TABLE_WIDTH_IN / height))


def table_png(label: str, title: str, subtitle: str, columns: list[tuple[str, float, str]], rows: list[list[str]]) -> bytes:
    """A table as a picture. Each column is (heading, relative width, "left" or "right"); the last one is highlighted."""
    if not rows or len(rows) > TABLE_MAX_ROWS or any(len(row) != len(columns) for row in rows):
        raise ValueError(f"a table takes 1 to {TABLE_MAX_ROWS} rows, each with one cell per column")
    height = 2.65 + TABLE_ROW_IN * len(rows)
    figure = Figure(figsize=(TABLE_WIDTH_IN, height), dpi=DPI, facecolor=TABLE_BACKGROUND)
    FigureCanvasAgg(figure)
    axes = figure.add_axes((0, 0, 1, 1))
    axes.set_axis_off()
    axes.set_xlim(0, 1)
    axes.set_ylim(0, 1)
    _spots(axes, height)

    left, right = 0.05, 0.95
    top = 1 - 0.30 / height
    axes.text(left, top, label.upper(), color=TABLE_BRAND, fontsize=10.5, fontweight="bold", fontfamily=FONT, va="top")
    axes.text(left, top - 0.34 / height, title, color=TABLE_INK, fontsize=21, fontweight="bold", fontfamily=FONT, va="top")
    axes.text(left, top - 0.86 / height, subtitle, color=TABLE_MUTED, fontsize=11.5, fontfamily=FONT, va="top")

    total = sum(width for _, width, _ in columns)
    anchors, edge = [], left
    for _, width, align in columns:
        span = (right - left) * width / total
        anchors.append(edge if align == "left" else edge + span)
        edge += span

    head = top - 1.45 / height
    for (heading, _, align), x in zip(columns, anchors):
        axes.text(x, head, heading.upper(), color=TABLE_MUTED, fontsize=9.5, fontweight="bold", fontfamily=FONT, ha=align, va="center")
    step = TABLE_ROW_IN / height
    line = head - step * 0.62
    axes.plot([left, right], [line, line], color=TABLE_RULE, linewidth=1.0)
    last = len(columns) - 1
    for n, row in enumerate(rows):
        y = line - step * (n + 0.5)
        for i, ((_, _, align), x, cell) in enumerate(zip(columns, anchors, row)):
            color = TABLE_MUTED if cell == EMPTY_CELL else TABLE_BRAND if i == last else TABLE_INK
            weight = "bold" if i in (0, last) else "normal"
            axes.text(x, y, cell, color=color, fontsize=13.5, fontweight=weight, fontfamily=FONT, ha=align, va="center")
        axes.plot([left, right], [y - step / 2] * 2, color=TABLE_RULE, linewidth=0.6)

    axes.text(left, 0.30 / height, TABLE_FOOTER, color=MUTED, fontsize=9, fontfamily=FONT, va="center")
    out = io.BytesIO()
    figure.savefig(out, format="png", facecolor=TABLE_BACKGROUND)
    return out.getvalue()
