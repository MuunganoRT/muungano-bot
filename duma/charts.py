"""Weekly charts, drawn here as a PNG held in memory.

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


def has_data(weeks: list[dict[str, Any]], metric: str) -> bool:
    if metric == "km":
        return any(w["distance_km"] for w in weeks)
    return any(w["prescribed"] for w in weeks)


def weekly_png(weeks: list[dict[str, Any]], metric: str, subtitle: str) -> bytes:
    """One chart of `metric` over the weeks the API returned, oldest first."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")

    # A Figure of its own, not pyplot: pyplot keeps global state and this runs off the event loop.
    figure = Figure(figsize=SIZE_IN, dpi=DPI, facecolor=BACKGROUND)
    FigureCanvasAgg(figure)
    axes = figure.add_subplot()
    axes.set_facecolor(BACKGROUND)

    xs = list(range(len(weeks)))
    labels = [_day(w["week_start"]) for w in weeks]

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

    axes.set_xticks(xs, labels)
    axes.set_xlim(-0.6, len(weeks) - 0.4)
    axes.tick_params(colors=MUTED, length=0, labelsize=10, labelfontfamily=FONT)
    axes.grid(axis="y", color=GRID, linewidth=1)
    axes.set_axisbelow(True)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(GRID)
    axes.set_xlabel("Semana del", color=MUTED, fontsize=10, fontfamily=FONT, labelpad=8)

    figure.text(0.06, 0.93, TITLES[metric], color=INK, fontsize=17, fontweight="bold", fontfamily=FONT)
    figure.text(0.06, 0.875, subtitle, color=MUTED, fontsize=11, fontfamily=FONT)
    figure.subplots_adjust(left=0.07, right=0.97, top=0.82, bottom=0.14)

    out = io.BytesIO()
    figure.savefig(out, format="png")
    return out.getvalue()


def _text() -> dict[str, Any]:
    return {"color": INK, "fontsize": 10, "fontfamily": FONT}
