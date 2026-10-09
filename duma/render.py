"""What Duma writes to the chat when a tool has its own answer.

Plain templates: the figures come from the API and no model rewrites them, so
the numbers that reach the admin are exactly the ones the API returned.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import date
from typing import Any, Optional

MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {MONTHS[d.month - 1]}"


def _day_year(iso: str) -> str:
    return f"{_day(iso)} {date.fromisoformat(iso).year}"


def _num(value: float) -> str:
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


def _period(start: str, end: str) -> str:
    if date.fromisoformat(start).year == date.fromisoformat(end).year:
        return f"{_day(start)} – {_day_year(end)}"
    return f"{_day_year(start)} – {_day_year(end)}"


def render_summary(data: dict[str, Any]) -> str:
    athlete, workouts, period = data["athlete"], data["workouts"], data["period"]
    cycle: Optional[dict[str, Any]] = data.get("cycle")

    title = athlete["name"] + ("" if athlete.get("active", True) else " (inactivo)")
    if cycle and cycle.get("current_week"):
        title += f" · {cycle['event']}, semana {cycle['current_week']} de {cycle['weeks_total']}"
    elif cycle:
        title += f" · {cycle['event']} ({_day_year(cycle['event_date'])})"

    lines = [title, _period(period["from"], period["to"])]

    prescribed, done = workouts["prescribed"], workouts["done"]
    # Prescribed for today or later and not run yet: neither done nor missed.
    pending = workouts.get("pending") or 0
    waiting = f"{pending} por hacer" if pending else ""
    if not prescribed:
        lines.append(f"Todavía sin entrenos que contar: {waiting}." if waiting else "Sin entrenos prescritos en ese periodo.")
        return "\n".join(lines)

    entry = f"Entrenos: {done}/{prescribed} ({round(100 * done / prescribed)}%)"
    if waiting:
        entry += f" · {waiting}"
    if data.get("score_avg") is not None:
        entry += f" · score {_num(data['score_avg'])}%"
    lines.append(entry)

    figures = []
    if data.get("distance_km"):
        figures.append(f"{_num(data['distance_km'])} km")
    if data.get("avg_pace"):
        figures.append(f"ritmo promedio {data['avg_pace']} min/km")
    if data.get("avg_heart_rate"):
        figures.append(f"FC promedio {_num(data['avg_heart_rate'])} lpm")
    if figures:
        lines.append(" · ".join(figures))

    longest = data.get("longest")
    if longest:
        parts = [f"{_num(longest['distance_km'])} km"]
        if longest.get("pace"):
            parts.append(f"{longest['pace']} min/km")
        if longest.get("heart_rate"):
            parts.append(f"{_num(longest['heart_rate'])} lpm")
        parts.append(f"score {_num(longest['score'])}%")
        lines.append(f"Más larga ({_day(longest['date'])}): " + " · ".join(parts))

    return "\n".join(lines)


def render_candidates(athletes: list[dict[str, Any]], total: int, question: bool) -> str:
    """The members found, without ids: the admin picks by name or group. Coaches and admins say so."""
    if not athletes:
        return "No encontré a nadie con ese nombre."
    lines = []
    if question:
        lines.append(f"Encontré {total} personas con ese nombre. ¿Cuál? Dime el nombre completo o el grupo.")
    else:
        lines.append(f"{total} persona(s):" if total != 1 else "1 persona:")
    for a in athletes:
        lines.append(f"- {_person(a)}")
    if total > len(athletes):
        lines.append(f"…y {total - len(athletes)} más; afina la búsqueda.")
    return "\n".join(lines)


def _person(a: dict[str, Any]) -> str:
    extra = []
    if a.get("role") in ("coach", "admin"):
        extra.append(a["role"].capitalize())
    if (a.get("group") or "").strip():
        extra.append(a["group"].strip())
    if not a.get("active", True):
        extra.append("inactivo")
    return a["name"] + (f" ({', '.join(extra)})" if extra else "")


def _race_time(value: Any) -> str:
    """A result as h:mm:ss. The API stores it in seconds; anything else is shown as it came."""
    if value in (None, ""):
        return ""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _goal_seconds(value: Any) -> Optional[int]:
    """The goal an athlete typed when entering a race, in seconds. None unless it reads as a time.

    The column is free text of up to 15 characters: only a clock time may reach the model.
    """
    match = re.fullmatch(r"(\d{1,2}):([0-5]\d)(?::([0-5]\d))?", str(value or "").strip())
    if not match:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return (hours * 3600 + minutes * 60 + seconds) or None


def _gap(goal: int, result: Any, words: bool = False) -> str:
    """Result minus goal, signed: `-0:02:45` is under the goal. In words for a spreadsheet, where a leading sign is a formula."""
    delta = int(result) - goal
    if words:
        return f"{_race_time(abs(delta))} {'menos' if delta < 0 else 'más'}" if delta else "en el objetivo"
    return ("-" if delta < 0 else "+" if delta else "") + _race_time(abs(delta))


def _money(amount: float) -> str:
    return f"${amount:,.0f} MXN" if abs(amount - round(amount)) < 0.005 else f"${amount:,.2f} MXN"


RECEIPT_STATUS = {"pending": "pendiente", "approved": "aprobado", "rejected": "rechazado"}


def _extras(a: dict[str, Any]) -> list[str]:
    """What the newer filters were about, as short phrases for one member's line."""
    parts = []
    if "covered_until" in a:
        parts.append(f"cubierto hasta {_day_year(a['covered_until'])}" if a["covered_until"] else "sin membresía")
    if a.get("member_since"):
        parts.append(f"alta {_day_year(a['member_since'])}")
    if a.get("paid_streak"):
        parts.append(f"{a['paid_streak']['months']} meses seguidos pagados (desde {_day_year(a['paid_streak']['since'])})")
    receipt = a.get("receipt")
    if receipt:
        kind = "beneficio" if receipt.get("benefit") else "comprobante"
        parts.append(f"{kind} {RECEIPT_STATUS.get(receipt['status'], receipt['status'])} ({_day_year(receipt['date'])})")
    if "watch" in a:
        if a.get("level") is not None:
            parts.append(f"nivel {a['level']}")
        if a.get("sede"):
            parts.append(a["sede"])
        parts.append("con reloj" if a["watch"] else "sin reloj")
    if "missed" in a:
        parts.append(f"{a['missed']} sin hacer")
    return parts


def render_matches(found: dict[str, Any]) -> str:
    """The members a filtered query matched, each with the columns its filters were about."""
    athletes, total = found["athletes"], found["total"]
    if not total:
        return "Nadie cumple esos filtros."
    lines = ["1 persona:" if total == 1 else f"{total} personas:"]
    for a in athletes:
        parts = [_person(a)]
        for e in a.get("events", []):
            entry = f"{e['event']} ({_day_year(e['date'])})"
            goal = _goal_seconds(e.get("goal"))
            if goal:
                entry += f" objetivo {_race_time(goal)}"
            if e.get("time_result"):
                entry += f" {'resultado ' if goal else ''}{_race_time(e['time_result'])}"
                if goal:
                    entry += f" ({_gap(goal, e['time_result'])})"
            parts.append(entry)
        payment = a.get("last_payment")
        if payment:
            amount = f"{_money(payment['amount'])} " if payment.get("amount") is not None else ""
            parts.append(f"pagó {amount}el {_day_year(payment['date'])}")
        parts += _extras(a)
        lines.append("- " + " · ".join(parts))
    if total > len(athletes):
        lines.append(f"…y {total - len(athletes)} más; acota los filtros.")
    return "\n".join(lines)


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    # A spreadsheet runs a cell that starts like a formula; names come from what members typed.
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def table_csv(header: list[str], rows: list[list[Any]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    # The BOM is what makes Excel read the accents as UTF-8.
    return out.getvalue().encode("utf-8-sig")


def matches_csv(found: dict[str, Any]) -> bytes:
    """The same list as `render_matches`, one member per row, for a spreadsheet."""
    athletes = found["athletes"]
    with_events = any(a.get("events") for a in athletes)
    with_payment = any(a.get("last_payment") for a in athletes)

    with_goals = any(_goal_seconds(e.get("goal")) for a in athletes for e in a.get("events") or [])

    header = ["Nombre", "Rol", "Grupo", "Estado"]
    if with_events:
        header += ["Evento", "Fecha del evento", "Tiempo"]
    if with_goals:
        header += ["Objetivo", "Diferencia"]
    if with_payment:
        header += ["Último pago", "Monto"]
    extras = [
        ("Cubierto hasta", "covered_until", lambda a: a.get("covered_until") or ""),
        ("Alta", "member_since", lambda a: a.get("member_since") or ""),
        ("Meses seguidos pagados", "paid_streak", lambda a: (a.get("paid_streak") or {}).get("months", "")),
        ("Comprobante", "receipt", lambda a: RECEIPT_STATUS.get(a["receipt"]["status"], "") if a.get("receipt") else ""),
        ("Beneficio", "receipt", lambda a: ("sí" if a["receipt"].get("benefit") else "no") if a.get("receipt") else ""),
        ("Nivel", "watch", lambda a: a.get("level") if a.get("level") is not None else ""),
        ("Sede", "watch", lambda a: a.get("sede") or ""),
        ("Reloj", "watch", lambda a: "sí" if a.get("watch") else "no"),
        ("Sin hacer", "missed", lambda a: a.get("missed", "")),
    ]
    extras = [(title, read) for title, key, read in extras if any(key in a for a in athletes)]
    header += [title for title, _ in extras]

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for a in athletes:
        row = [a["name"], a.get("role") or "", (a.get("group") or "").strip(), "activo" if a.get("active", True) else "inactivo"]
        if with_events:
            events = a.get("events") or []
            row += [
                "; ".join(e["event"] for e in events),
                "; ".join(e["date"] for e in events),
                "; ".join(_race_time(e.get("time_result")) for e in events),
            ]
            if with_goals:
                goals = [_goal_seconds(e.get("goal")) for e in events]
                row += [
                    "; ".join(_race_time(g) for g in goals),
                    "; ".join(_gap(g, e["time_result"], words=True) if g and e.get("time_result") else "" for g, e in zip(goals, events)),
                ]
        if with_payment:
            payment = a.get("last_payment") or {}
            row += [payment.get("date") or "", "" if payment.get("amount") is None else payment["amount"]]
        row += [read(a) for _, read in extras]
        writer.writerow([_cell(v) for v in row])
    # The BOM is what makes Excel read the accents as UTF-8.
    return out.getvalue().encode("utf-8-sig")
