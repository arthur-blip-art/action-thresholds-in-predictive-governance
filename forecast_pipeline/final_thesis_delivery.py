"""Final thesis delivery outputs built from existing local CSV artifacts only.

This module intentionally avoids any new data collection. It converts the
already-validated Fed dec-to-Apr and Anthropic outputs into final thesis-ready
figures and short interpretation notes.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

from .io_utils import ensure_dir, read_csv_rows
from .thesis_analysis import _ChartBackend, _draw_axes, _load_font, _wrap_text


FED_MEETINGS = ("2025-12", "2026-01", "2026-03", "2026-04")
FED_MEETING_LABELS = {
    "2025-12": "December 2025",
    "2026-01": "January 2026",
    "2026-03": "March 2026",
    "2026-04": "April 2026",
}
FED_OUTCOME_ORDER = ("50_plus_bps_cut", "25_bps_cut", "no_change", "25_plus_bps_hike")
FED_OUTCOME_LABELS = {
    "50_plus_bps_cut": "50+ bps cut",
    "25_bps_cut": "25 bps cut",
    "no_change": "No change",
    "25_plus_bps_hike": "25+ bps hike",
}
FED_COLORS = {
    "50_plus_bps_cut": "#b0b5bd",
    "25_bps_cut": "#6f7886",
    "no_change": "#2f3e4e",
    "25_plus_bps_hike": "#0b1f3a",
}
FED_REALIZED_COLOR = "#0b1f3a"
FED_CONTEXT_COLOR = "#0b1f3a"
FED_BAND_COLOR = "#dde2e9"

ANTHROPIC_FULL_ORDER = (
    "LOW 750B",
    "LOW 800B",
    "LOW 850B",
    "LOW 875B",
    "HIGH 1.0T",
    "HIGH 1.1T",
    "HIGH 1.25T",
    "HIGH 1.5T",
    "HIGH 1.75T",
)
ANTHROPIC_CORE_ORDER = (
    "LOW 750B",
    "LOW 850B",
    "LOW 875B",
    "HIGH 1.0T",
    "HIGH 1.25T",
    "HIGH 1.75T",
)
ANTHROPIC_COLORS = {
    "LOW 750B": "#0b1f3a",
    "LOW 800B": "#25364a",
    "LOW 850B": "#3f4b5d",
    "LOW 875B": "#576170",
    "HIGH 1.0T": "#6f7886",
    "HIGH 1.1T": "#87909b",
    "HIGH 1.25T": "#9ea6af",
    "HIGH 1.5T": "#b6bcc4",
    "HIGH 1.75T": "#ced3d8",
}


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except Exception:
        return None


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _format_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value * 100:.1f}%"


def _meeting_sort_key(meeting_month: str) -> tuple[int, int]:
    try:
        year, month = meeting_month.split("-")
        return int(year), int(month)
    except Exception:
        return (9999, 99)


def _endpoint_type(source_endpoint: str) -> str:
    return "trades_fallback" if "trades" in source_endpoint else "prices_history"


def _anthropic_threshold_label(question: str) -> str:
    text = question.replace("’", "'")
    match = re.search(r"\((LOW|HIGH)\)\s*\$([0-9.]+)([TB])", text, flags=re.I)
    if not match:
        return question
    direction = match.group(1).upper()
    amount = float(match.group(2))
    unit = match.group(3).upper()
    if unit == "B":
        amount_text = f"{int(round(amount))}B"
    else:
        amount_text = f"{amount:.1f}T" if amount.is_integer() else f"{amount:g}T"
    return f"{direction} {amount_text}"


def _load_csv(path: Path) -> list[dict[str, Any]]:
    return read_csv_rows(path) if path.exists() else []


def _load_fed_context(base_dir: Path) -> dict[str, Any]:
    comparator_rows = _load_csv(base_dir / "analysis" / "comparators" / "fed_realized_decisions_dec_to_apr.csv")
    snapshot_rows = _load_csv(base_dir / "analysis" / "snapshots" / "fed_dec_to_apr_snapshots.csv")
    daily_rows = _load_csv(base_dir / "analysis" / "daily" / "fed_dec_to_apr_daily.csv")
    summary_rows = _load_csv(base_dir / "analysis" / "tables" / "fed_case_selection_summary_dec_to_apr.csv")
    nyfed_rows = _load_csv(base_dir / "analysis" / "comparators" / "nyfed_effr_dec_to_apr.csv")
    long_rows = _load_csv(base_dir / "pipeline_outputs" / "cleaned" / "polymarket_fed_decisions_dec_to_apr_long.csv")
    return {
        "comparator_rows": comparator_rows,
        "snapshot_rows": snapshot_rows,
        "daily_rows": daily_rows,
        "summary_rows": summary_rows,
        "nyfed_rows": nyfed_rows,
        "long_rows": long_rows,
    }


def _load_anthropic_context(base_dir: Path) -> dict[str, Any]:
    daily_rows = _load_csv(base_dir / "analysis" / "daily" / "anthropic_valuation_daily.csv")
    snapshot_rows = _load_csv(base_dir / "analysis" / "snapshots" / "anthropic_valuation_snapshots.csv")
    long_rows = _load_csv(base_dir / "pipeline_outputs" / "cleaned" / "polymarket_anthropic_price_history_long.csv")
    return {
        "daily_rows": daily_rows,
        "snapshot_rows": snapshot_rows,
        "long_rows": long_rows,
    }


def _probability_rows_by_meeting(daily_rows: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    meetings: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for row in daily_rows:
        meeting_month = str(row.get("meeting_month", "")).strip()
        if meeting_month not in FED_MEETINGS:
            continue
        label = str(row.get("normalized_outcome_label", "")).strip()
        meetings[meeting_month][label].append(row)
    return meetings


def _realized_labels(comparator_rows: list[dict[str, Any]]) -> dict[str, str]:
    out = {}
    for row in comparator_rows:
        out[str(row.get("meeting_month", "")).strip()] = str(row.get("realized_outcome_label", "")).strip()
    return out


def _final_snapshot_probability(snapshot_rows: list[dict[str, Any]], meeting_month: str, outcome_label: str) -> float | None:
    rows = [
        row
        for row in snapshot_rows
        if str(row.get("meeting_month", "")).strip() == meeting_month
        and str(row.get("snapshot_label", "")).strip() == "final_available"
        and str(row.get("normalized_outcome_label", "")).strip() == outcome_label
    ]
    if not rows:
        return None
    return _safe_float(rows[0].get("probability"))


def _meeting_panel_title(meeting_month: str, realized_label: str) -> str:
    return f"{FED_MEETING_LABELS.get(meeting_month, meeting_month)} | realized: {FED_OUTCOME_LABELS.get(realized_label, realized_label)}"


def _render_small_multiples(
    output_base: Path,
    title: str,
    subtitle: str,
    panels: list[dict[str, Any]],
    note: str,
    x_axis_label: str,
) -> None:
    width = 1600
    panel_h = 240
    top_margin = 105
    bottom_margin = 70
    height = top_margin + bottom_margin + panel_h * len(panels)
    left = 110
    right = 1160
    legend_left = 1210
    backends = [_ChartBackend("png", width, height), _ChartBackend("svg", width, height)]
    fonts = {
        "title": _load_font(28, bold=True),
        "body": _load_font(18),
        "small": _load_font(15),
        "tiny": _load_font(13),
    }

    for backend in backends:
        backend.rect((0, 0, width, height), fill="white", outline=None)
        backend.text((20, 24), title, fonts["title"], fill="#0b1f3a", anchor="la")
        backend.text((20, 58), subtitle, fonts["body"], fill="#333333", anchor="la")
        backend.text((legend_left, 58), "Series", fonts["body"], fill="#0b1f3a", anchor="la")

        for panel_index, panel in enumerate(panels):
            meeting_month = panel["meeting_month"]
            data = panel["data"]
            realized_label = panel["realized_label"]
            panel_top = top_margin + panel_index * panel_h
            panel_bottom = panel_top + 155
            _draw_axes(backend, left, panel_top, right, panel_bottom)

            x_vals = [int(row["days_to_meeting"]) for rows in data.values() for row in rows if str(row.get("days_to_meeting", "")).strip()]
            if not x_vals:
                continue
            x_max = max(x_vals)
            x_min = 0
            if x_max == x_min:
                x_max = x_min + 1

            def x_to_px(x: int) -> float:
                span = x_max - x_min or 1
                return right - ((x - x_min) / float(span)) * (right - left)

            def y_to_px(y: float) -> float:
                y = max(0.0, min(1.0, y))
                return panel_bottom - y * (panel_bottom - panel_top)

            for tick in [0.0, 0.25, 0.5, 0.75, 1.0]:
                backend.text((left - 12, y_to_px(tick)), f"{tick:.2f}".rstrip("0").rstrip("."), fonts["tiny"], fill="#666666", anchor="ra")
            if x_max > 120:
                tick_vals = [x_max, int(round(x_max * 0.75)), int(round(x_max * 0.5)), int(round(x_max * 0.25)), 0]
            elif x_max > 30:
                tick_vals = [x_max, 30, 14, 7, 3, 1, 0]
            else:
                tick_vals = [x_max, 7, 3, 1, 0]
            seen: set[int] = set()
            for tick in tick_vals:
                if tick < x_min or tick > x_max or tick in seen:
                    continue
                seen.add(tick)
                backend.text((x_to_px(tick), panel_bottom + 12), str(tick), fonts["tiny"], fill="#666666", anchor="ma")

            ordered_labels = [label for label in FED_OUTCOME_ORDER if label in data]
            legend_y = panel_top
            for label in ordered_labels:
                series = sorted(data[label], key=lambda row: int(row["days_to_meeting"]))
                points = []
                for row in series:
                    prob = _safe_float(row.get("last_probability"))
                    days = _safe_float(row.get("days_to_meeting"))
                    if prob is None or days is None:
                        continue
                    points.append((x_to_px(int(days)), y_to_px(prob)))
                if not points:
                    continue
                color = FED_REALIZED_COLOR if label == realized_label else FED_COLORS.get(label, "#7a7f87")
                width_px = 4 if label == realized_label else 2
                for idx in range(len(points) - 1):
                    backend.line(points[idx], points[idx + 1], color, width_px)
                for pt in points:
                    backend.circle(pt, 3.5 if label == realized_label else 3.0, fill=color, outline="white", width=1)
                backend.line((legend_left, legend_y + 9), (legend_left + 28, legend_y + 9), color, 4 if label == realized_label else 3)
                backend.circle((legend_left + 14, legend_y + 9), 4, fill=color, outline="white", width=1)
                backend.text((legend_left + 40, legend_y + 9), FED_OUTCOME_LABELS.get(label, label), fonts["small"], fill="#30343A", anchor="la")
                legend_y += 28

            backend.text((left, panel_top - 16), _meeting_panel_title(meeting_month, realized_label), fonts["body"], fill="#0b1f3a", anchor="la")
            backend.text((left, panel_bottom + 32), f"{meeting_month} | trade-based fallback series", fonts["small"], fill="#4a4f57", anchor="la")

        wrapped_note = "\n".join(_wrap_text(note, fonts["small"], right - left + 360, backend))
        backend.rect((left, height - 55, width - 35, height - 12), fill="#fafafa", outline="#d6dbe2", width=1, rx=6)
        backend.multiline_text((left + 14, height - 48), wrapped_note, fonts["small"], fill="#333333", spacing=5)
        backend.text((left, height - 72), x_axis_label, fonts["small"], fill="#4a4f57", anchor="la")

    backends[0].save(output_base.with_suffix(".png"))
    backends[1].save(output_base.with_suffix(".svg"))


def _render_snapshot_accuracy(
    output_base: Path,
    comparator_rows: list[dict[str, Any]],
    snapshot_rows: list[dict[str, Any]],
) -> None:
    realized = _realized_labels(comparator_rows)
    horizons = ["T-30", "T-14", "T-7", "T-3", "T-1", "final_available"]
    by_meeting: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot_rows:
        meeting_month = str(row.get("meeting_month", "")).strip()
        if meeting_month in FED_MEETINGS and str(row.get("normalized_outcome_label", "")).strip() == realized.get(meeting_month, ""):
            by_meeting[meeting_month].append(row)
    meeting_months = [m for m in FED_MEETINGS if m in by_meeting]
    if not meeting_months:
        return

    width, height = 1500, 760
    left, top, right, bottom = 110, 105, 1150, 560
    legend_left = 1200
    backends = [_ChartBackend("png", width, height), _ChartBackend("svg", width, height)]
    fonts = {"title": _load_font(28, bold=True), "body": _load_font(18), "small": _load_font(15), "tiny": _load_font(13)}
    colors = ["#0b1f3a", "#2f3e4e", "#5c677d", "#7a7f87"]
    slot_width = (right - left) / max(len(horizons), 1)
    horizon_index = {label: idx for idx, label in enumerate(horizons)}

    for backend in backends:
        backend.rect((0, 0, width, height), fill="white", outline=None)
        backend.text((20, 24), "Fed snapshot accuracy", fonts["title"], fill="#0b1f3a", anchor="la")
        backend.text((20, 58), "Probability assigned to the realized outcome by snapshot horizon", fonts["body"], fill="#333333", anchor="la")
        _draw_axes(backend, left, top, right, bottom)

        for tick in [0.0, 0.25, 0.5, 0.75, 1.0]:
            backend.text((left - 12, bottom - (bottom - top) * tick), f"{tick:.2f}".rstrip("0").rstrip("."), fonts["tiny"], fill="#666666", anchor="ra")
        for idx, label in enumerate(horizons):
            backend.text((left + idx * slot_width + slot_width / 2.0, bottom + 12), label, fonts["tiny"], fill="#666666", anchor="ma")

        for meeting_idx, meeting_month in enumerate(meeting_months):
            rows = by_meeting[meeting_month]
            points = []
            for label in horizons:
                row = next((r for r in rows if str(r.get("snapshot_label", "")).strip() == label), None)
                if row is None:
                    continue
                prob = _safe_float(row.get("probability"))
                if prob is None:
                    continue
                x = left + horizon_index[label] * slot_width + slot_width / 2.0
                y = bottom - prob * (bottom - top)
                points.append((x, y, prob, label))
            for idx in range(len(points) - 1):
                backend.line((points[idx][0], points[idx][1]), (points[idx + 1][0], points[idx + 1][1]), colors[meeting_idx % len(colors)], 3)
            for x, y, prob, label in points:
                backend.circle((x, y), 4.5, fill=colors[meeting_idx % len(colors)], outline="white", width=1)
                backend.text((x, y - 12), f"{prob:.3f}", fonts["tiny"], fill="#444444", anchor="ma")
            backend.line((legend_left, top + meeting_idx * 28 + 9), (legend_left + 24, top + meeting_idx * 28 + 9), colors[meeting_idx % len(colors)], 4)
            backend.circle((legend_left + 12, top + meeting_idx * 28 + 9), 4, fill=colors[meeting_idx % len(colors)], outline="white", width=1)
            backend.text((legend_left + 34, top + meeting_idx * 28 + 9), FED_MEETING_LABELS.get(meeting_month, meeting_month), fonts["small"], fill="#30343A", anchor="la")

        backend.text((legend_left, top + len(meeting_months) * 28 + 18), "Higher is better", fonts["small"], fill="#4a4f57", anchor="la")
        backend.text((left, height - 55), "Equivalent Brier score for the realized outcome is (1 - p)^2 and is near zero when p is near one.", fonts["small"], fill="#4a4f57", anchor="la")
        backend.text((left, height - 75), "Missing horizons are omitted rather than fabricated.", fonts["small"], fill="#4a4f57", anchor="la")

    backends[0].save(output_base.with_suffix(".png"))
    backends[1].save(output_base.with_suffix(".svg"))


def _render_realized_rates(
    output_base: Path,
    comparator_rows: list[dict[str, Any]],
    nyfed_rows: list[dict[str, Any]],
) -> None:
    if not comparator_rows:
        return
    width, height = 1600, 760
    left, top, right, bottom = 120, 105, 1220, 560
    legend_left = 1260
    backends = [_ChartBackend("png", width, height), _ChartBackend("svg", width, height)]
    fonts = {"title": _load_font(28, bold=True), "body": _load_font(18), "small": _load_font(15), "tiny": _load_font(13)}

    target_rows = []
    for row in comparator_rows:
        meeting_dt = _parse_date(str(row.get("meeting_date", "")))
        low = _safe_float(str(row.get("target_range_after", "")).split("-")[0])
        high = _safe_float(str(row.get("target_range_after", "")).split("-")[-1])
        if meeting_dt is None or low is None or high is None:
            continue
        target_rows.append((meeting_dt, low, high, str(row.get("meeting_month", "")), str(row.get("target_range_after", ""))))
    if not target_rows:
        return

    nyfed_series = []
    for row in nyfed_rows:
        day = _parse_date(str(row.get("date", "")))
        rate = _safe_float(row.get("rate_value"))
        if day is None or rate is None:
            continue
        nyfed_series.append((day, rate))
    date_values = [t[0] for t in target_rows] + [d for d, _ in nyfed_series]
    date_min, date_max = min(date_values), max(date_values)
    if date_min == date_max:
        date_max = date_max + timedelta(days=1)
    y_min, y_max = 3.45, 3.85

    def x_to_px(day: date) -> float:
        span = (date_max - date_min).days or 1
        return left + ((day - date_min).days / float(span)) * (right - left)

    def y_to_px(rate: float) -> float:
        rate = max(y_min, min(y_max, rate))
        return bottom - ((rate - y_min) / float(y_max - y_min)) * (bottom - top)

    for backend in backends:
        backend.rect((0, 0, width, height), fill="white", outline=None)
        backend.text((20, 24), "Realized rate context", fonts["title"], fill="#0b1f3a", anchor="la")
        backend.text((20, 58), "Official target range after each meeting and the accessible NY Fed EFFR rows", fonts["body"], fill="#333333", anchor="la")
        _draw_axes(backend, left, top, right, bottom)

        for tick in [3.50, 3.60, 3.70, 3.80]:
            backend.text((left - 12, y_to_px(tick)), f"{tick:.2f}", fonts["tiny"], fill="#666666", anchor="ra")
        tick_dates = [date_min, date_min + timedelta(days=(date_max - date_min).days // 3), date_min + timedelta(days=2 * (date_max - date_min).days // 3), date_max]
        for tick in tick_dates:
            backend.text((x_to_px(tick), bottom + 12), tick.isoformat(), fonts["tiny"], fill="#666666", anchor="ma")

        target_rows_sorted = sorted(target_rows, key=lambda item: item[0])
        for idx, (meeting_dt, low, high, meeting_month, target_after) in enumerate(target_rows_sorted):
            next_dt = target_rows_sorted[idx + 1][0] if idx + 1 < len(target_rows_sorted) else date_max
            backend.rect((x_to_px(meeting_dt), y_to_px(high), x_to_px(next_dt), y_to_px(low)), fill=FED_BAND_COLOR, outline="#c7cdd6", width=1)
            backend.line((x_to_px(meeting_dt), top), (x_to_px(meeting_dt), bottom), "#7a7f87", 1)
            backend.text((x_to_px(meeting_dt) + 4, y_to_px(high) - 8), f"{meeting_month} target {target_after}", fonts["tiny"], fill="#23324a", anchor="la")

        if nyfed_series:
            pts = [(x_to_px(day), y_to_px(rate)) for day, rate in nyfed_series]
            for idx in range(len(pts) - 1):
                backend.line(pts[idx], pts[idx + 1], FED_CONTEXT_COLOR, 3)
            for pt in pts:
                backend.circle(pt, 3.5, fill=FED_CONTEXT_COLOR, outline="white", width=1)

        backend.line((legend_left, top + 10), (legend_left + 24, top + 10), FED_BAND_COLOR, 10)
        backend.text((legend_left + 36, top + 10), "Target range after meeting", fonts["small"], fill="#30343A", anchor="la")
        backend.line((legend_left, top + 38), (legend_left + 24, top + 38), FED_CONTEXT_COLOR, 3)
        backend.circle((legend_left + 12, top + 38), 3.5, fill=FED_CONTEXT_COLOR, outline="white", width=1)
        backend.text((legend_left + 36, top + 38), "EFFR", fonts["small"], fill="#30343A", anchor="la")
        backend.text((legend_left, top + 72), "Context only, not forecast data.", fonts["small"], fill="#4a4f57", anchor="la")
        backend.text((left, height - 55), "Realized-rate context from the NY Fed reference-rates page; it is not used as a probability benchmark.", fonts["small"], fill="#4a4f57", anchor="la")

    backends[0].save(output_base.with_suffix(".png"))
    backends[1].save(output_base.with_suffix(".svg"))


def _render_series_chart(
    output_base: Path,
    title: str,
    subtitle: str,
    series_data: dict[str, list[tuple[datetime, float]]],
    note: str,
    color_map: dict[str, str],
    legend_order: list[str],
    x_label: str,
    y_label: str,
) -> None:
    width, height = 1500, 880
    left, top, right, bottom = 110, 105, 1110, 675
    legend_left = 1160
    backends = [_ChartBackend("png", width, height), _ChartBackend("svg", width, height)]
    fonts = {"title": _load_font(28, bold=True), "body": _load_font(18), "small": _load_font(15), "tiny": _load_font(13)}

    all_points = [(x, y) for series in series_data.values() for x, y in series]
    if not all_points:
        return
    x_min = min(x for x, _ in all_points)
    x_max = max(x for x, _ in all_points)
    if x_min == x_max:
        x_max = x_max + timedelta(days=1)

    def x_to_px(x: datetime) -> float:
        total = (x_max - x_min).total_seconds() or 1.0
        return left + ((x - x_min).total_seconds() / total) * (right - left)

    def y_to_px(y: float) -> float:
        y = max(0.0, min(1.0, y))
        return bottom - y * (bottom - top)

    for backend in backends:
        backend.rect((0, 0, width, height), fill="white", outline=None)
        backend.text((left, 24), title, fonts["title"], fill="#0b1f3a", anchor="la")
        backend.text((left, 60), subtitle, fonts["body"], fill="#333333", anchor="la")
        _draw_axes(backend, left, top, right, bottom)
        backend.text(((left + right) / 2.0, height - 115), x_label, fonts["body"], fill="#333333", anchor="ma")
        backend.text((18, (top + bottom) / 2.0), y_label, fonts["body"], fill="#333333", anchor="la")

        for tick in [0.0, 0.25, 0.5, 0.75, 1.0]:
            backend.text((left - 12, y_to_px(tick)), f"{tick:.2f}".rstrip("0").rstrip("."), fonts["tiny"], fill="#666666", anchor="ra")

        total_days = max((x_max - x_min).days, 1)
        if total_days <= 2:
            ticks = [x_min, x_max]
        elif total_days <= 20:
            ticks = [x_min, x_min + (x_max - x_min) / 2, x_max]
        else:
            ticks = [x_min + (x_max - x_min) * i / 4 for i in range(5)]
        for tick in ticks:
            label = tick.strftime("%Y-%m-%d") if total_days > 2 else tick.strftime("%Y-%m-%d")
            backend.text((x_to_px(tick), bottom + 12), label, fonts["tiny"], fill="#666666", anchor="ma")

        for label in legend_order:
            series = series_data.get(label)
            if not series:
                continue
            color = color_map.get(label, "#0b1f3a")
            ordered = sorted(series, key=lambda item: item[0])
            points = [(x_to_px(x), y_to_px(y)) for x, y in ordered]
            for idx in range(len(points) - 1):
                backend.line(points[idx], points[idx + 1], color, 3)
            for pt in points:
                backend.circle(pt, 3.5, fill=color, outline="white", width=1)

        backend.text((legend_left, top), "Series", fonts["body"], fill="#0b1f3a", anchor="la")
        legend_y = top + 40
        for label in legend_order:
            if label not in series_data:
                continue
            color = color_map.get(label, "#0b1f3a")
            backend.line((legend_left, legend_y + 10), (legend_left + 30, legend_y + 10), color, 4)
            backend.circle((legend_left + 15, legend_y + 10), 4, fill=color, outline="white", width=1)
            backend.text((legend_left + 42, legend_y + 10), label, fonts["small"], fill="#333333", anchor="la")
            legend_y += 30

        wrapped = "\n".join(_wrap_text(note, fonts["small"], right - left + 360, backend))
        backend.rect((left, height - 95, right, height - 30), fill="white", outline="#cccccc", width=1, rx=8)
        backend.multiline_text((left + 14, height - 82), wrapped, fonts["small"], fill="#333333", spacing=5)

    backends[0].save(output_base.with_suffix(".png"))
    backends[1].save(output_base.with_suffix(".svg"))


def _render_table(
    output_base: Path,
    title: str,
    columns: list[str],
    rows: list[list[str]],
    col_widths: list[int],
    note: str | None = None,
) -> None:
    width = sum(col_widths) + 160
    row_font = _load_font(15)
    title_font = _load_font(26, bold=True)
    header_font = _load_font(15, bold=True)
    small_font = _load_font(13)

    # Estimate row height after wrapping.
    line_height = getattr(row_font, "size", 15) + 6
    wrapped_rows: list[list[list[str]]] = []
    row_heights: list[int] = []
    for row in rows:
        wrapped_row = []
        heights = []
        for idx, cell in enumerate(row):
            backend = _ChartBackend("svg", width, 1000)
            lines = _wrap_text(cell, row_font, col_widths[idx] - 18, backend)
            wrapped_row.append(lines)
            heights.append(max(1, len(lines)) * line_height)
        wrapped_rows.append(wrapped_row)
        row_heights.append(max(heights) + 10)

    note_lines = []
    if note:
        note_backend = _ChartBackend("svg", width, 1000)
        note_lines = _wrap_text(note, small_font, width - 120, note_backend)
    height = 90 + 44 + sum(row_heights) + (len(note_lines) * 20 + 50 if note_lines else 0)

    backends = [_ChartBackend("png", width, height), _ChartBackend("svg", width, height)]
    for backend in backends:
        backend.rect((0, 0, width, height), fill="white", outline=None)
        backend.text((40, 24), title, title_font, fill="#0b1f3a", anchor="la")
        x0 = 40
        y = 74
        table_right = x0 + sum(col_widths)
        backend.rect((x0, y, table_right, y + 34), fill="#0b1f3a", outline="#0b1f3a", width=1)
        x = x0
        for idx, col in enumerate(columns):
            w = col_widths[idx]
            backend.text((x + 8, y + 17), col, header_font, fill="white", anchor="lm")
            x += w
        y += 34
        for row_idx, row in enumerate(rows):
            row_h = row_heights[row_idx]
            fill = "#f7f9fb" if row_idx % 2 == 0 else "#ffffff"
            backend.rect((x0, y, table_right, y + row_h), fill=fill, outline="#d7dce2", width=1)
            x = x0
            for col_idx, cell in enumerate(row):
                w = col_widths[col_idx]
                lines = wrapped_rows[row_idx][col_idx]
                backend.multiline_text((x + 8, y + 8), "\n".join(lines), row_font, fill="#222222", spacing=4)
                backend.line((x + w, y), (x + w, y + row_h), "#d7dce2", 1)
                x += w
            y += row_h
        if note_lines:
            backend.rect((40, height - 70, width - 40, height - 16), fill="#fafafa", outline="#d7dce2", width=1, rx=6)
            backend.multiline_text((54, height - 58), "\n".join(note_lines), small_font, fill="#333333", spacing=4)
    backends[0].save(output_base.with_suffix(".png"))
    backends[1].save(output_base.with_suffix(".svg"))


def _fed_table_rows(context: dict[str, Any]) -> list[list[str]]:
    comparator_rows = context["comparator_rows"]
    snapshot_rows = context["snapshot_rows"]
    summary_rows = {str(row.get("meeting_month", "")).strip(): row for row in context["summary_rows"]}
    realized = _realized_labels(comparator_rows)
    out = []
    for row in comparator_rows:
        meeting_month = str(row.get("meeting_month", "")).strip()
        realized_label = realized.get(meeting_month, "")
        final_prob = _final_snapshot_probability(snapshot_rows, meeting_month, realized_label)
        out.append(
            [
                FED_MEETING_LABELS.get(meeting_month, meeting_month),
                str(row.get("official_decision", "")),
                _format_pct(final_prob),
                str(summary_rows.get(meeting_month, {}).get("source_endpoint_type", "trades_fallback")),
                str(summary_rows.get(meeting_month, {}).get("caveat_methodologique", "")),
            ]
        )
    return out


def _anthropic_table_rows(context: dict[str, Any]) -> list[list[str]]:
    daily_rows = context["daily_rows"]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        label = _anthropic_threshold_label(str(row.get("market_question", "")))
        grouped[label].append(row)
    out = []
    for label in sorted(grouped.keys(), key=lambda x: ANTHROPIC_FULL_ORDER.index(x) if x in ANTHROPIC_FULL_ORDER else 999):
        rows = sorted(grouped[label], key=lambda r: str(r.get("date", "")))
        probs = [_safe_float(r.get("last_probability")) for r in rows]
        probs = [p for p in probs if p is not None]
        out.append(
            [
                label,
                f"{probs[0]:.3f}" if probs else "",
                f"{probs[-1]:.3f}" if probs else "",
                f"{min(probs):.3f}" if probs else "",
                f"{max(probs):.3f}" if probs else "",
                "threshold probability series tied to institutional reference data",
            ]
        )
    return out


def _write_notes(base_dir: Path, fed_context: dict[str, Any], anthropic_context: dict[str, Any]) -> dict[str, Path]:
    notes_dir = ensure_dir(base_dir / "analysis" / "notes")

    comparator_rows = fed_context["comparator_rows"]
    snapshot_rows = fed_context["snapshot_rows"]
    daily_rows = fed_context["daily_rows"]
    summary_rows = fed_context["summary_rows"]
    realized = _realized_labels(comparator_rows)

    fed_lines = [
        "# Fed Final Interpretation",
        "",
        "## What the Figure Set Shows",
        "The final Fed section compares ex ante Polymarket probabilities before each exact FOMC decision with the realized Federal Reserve policy outcome. The four included meetings are December 2025, January 2026, March 2026, and April 2026.",
        "",
        "## What Is Empirically Measured",
        "- All four meetings are reconstructed from Polymarket trade-level fallback series.",
        "- That means the series are transaction-implied probabilities, not standardized prices-history endpoints.",
        "- The charts and snapshots are nevertheless comparable because the same event family and outcome labels are used across meetings.",
        "",
        "## Main Takeaway",
    ]
    for row in comparator_rows:
        meeting_month = str(row.get("meeting_month", "")).strip()
        target = realized.get(meeting_month, "")
        prob = _final_snapshot_probability(snapshot_rows, meeting_month, target)
        fed_lines.append(
            f"- {FED_MEETING_LABELS.get(meeting_month, meeting_month)}: the realized outcome ends at {_format_pct(prob)} in the final available snapshot, and the market is effectively resolved by the meeting date."
        )
    fed_lines.extend(
        [
            "",
            "Across the recovered horizons, convergence is early enough to matter: where T-30/T-14/T-7 data exist, the realized outcome is already dominant or becomes dominant well before the meeting. This supports a decision-relevance reading rather than a pure ex post correctness story.",
            "",
            "## Methodological Caveat",
            "The section is not a prices-history benchmark study. It is a trade-level fallback reconstruction of Polymarket probabilities. That is still suitable for a thesis figure, but the caveat must remain visible in the caption and prose.",
            "",
            "## What Can and Cannot Be Claimed",
            "- Can claim: Polymarket probabilities moved toward the realized Fed decision sufficiently early to be decision-relevant in the recovered series.",
            "- Can claim: the four exact FOMC meetings are now comparable in a single cleaned panel.",
            "- Cannot overclaim: the section is not a direct comparison against another forecast source, and it is not a standardized prices-history analysis.",
            "",
        ]
    )
    fed_path = notes_dir / "fed_final_interpretation.md"
    fed_path.write_text("\n".join(fed_lines).strip() + "\n", encoding="utf-8")

    # Anthropic note
    anthropic_lines = [
        "# Anthropic Final Interpretation",
        "",
        "## Why This Case Matters",
        "Anthropic is not a classic forecast-accuracy case. Polymarket is not trying to value Anthropic directly; it prices the probability that discrete valuation thresholds are crossed by June 30.",
        "",
        "## What the Figure Shows",
        "- The market produces a ladder of threshold probabilities rather than a single yes/no forecast.",
        "- Lower valuation thresholds are priced as far more likely than the higher ones, which is exactly the kind of ordering an investor can use as an actionability input.",
        "- The series is therefore best read as a threshold surface over uncertainty, not as a point forecast.",
        "",
        "## Reference Data, Not Competing Forecasts",
        "Nasdaq Private Market and SecondMarket should be treated as institutional reference data for the valuation regime, not as another probability source. Forge and Secondary Suite can be mentioned as secondary-market signals that help contextualize the environment, but not as benchmarks that Polymarket must 'beat'.",
        "",
        "## Actionability Logic",
        "An investor can compare threshold probabilities to entry valuation, liquidity constraints, and a required conviction threshold. That is the thesis contribution: the market may be useful precisely because it translates opaque private-market information into actionable bands.",
        "",
        "## Claim Discipline",
        "- Can claim: the chart operationalizes valuation uncertainty as threshold probabilities.",
        "- Can claim: the case is useful for studying decision thresholds and timing, not just accuracy.",
        "- Cannot overclaim: the series is not a direct Anthropic valuation model and does not replace institutional reference data.",
        "",
    ]
    anthropic_path = notes_dir / "anthropic_final_interpretation.md"
    anthropic_path.write_text("\n".join(anthropic_lines).strip() + "\n", encoding="utf-8")

    guide_lines = [
        "# Stylized Facts Writing Guide",
        "",
        "## 1. Fed Stylized Fact",
        "**Empirical setup.** Exact Polymarket Fed decision markets for December 2025, January 2026, March 2026, and April 2026; all recovered as trade-based fallback series.",
        "**What the chart shows.** Probability paths move rapidly toward the realized FOMC outcome and are already close to fully resolved by the last available pre-meeting snapshots.",
        "**Key takeaway.** The market appears decision-relevant before resolution, not merely correct after the fact.",
        "**Methodological caveat.** These are transaction-implied fallback probabilities, not standardized prices-history endpoints.",
        "",
        "## 2. Anthropic Stylized Fact",
        "**Empirical setup.** Polymarket threshold markets for Anthropic valuation crossing discrete bands by June 30.",
        "**What the chart shows.** The probability surface is ordered by threshold: low bands are far more likely than high bands, and the series evolves as a set of valuation-crossing probabilities rather than a single forecast.",
        "**Key takeaway.** The useful object is not accuracy in the usual sense but whether threshold probabilities make a private-market decision actionable.",
        "**Theoretical contribution.** This turns prediction-market data into a framework for studying investment conviction under opaque valuation conditions.",
        "",
        "## 3. Bridge Paragraph",
        "Fed illustrates ex ante convergence toward a realized institutional outcome. Anthropic illustrates threshold-based actionability in an opaque private market. Together they show two uses of prediction-market probabilities: one for timing and convergence, one for decision thresholds under uncertainty.",
        "",
    ]
    guide_path = notes_dir / "stylized_facts_writing_guide.md"
    guide_path.write_text("\n".join(guide_lines).strip() + "\n", encoding="utf-8")

    status_lines = [
        "# Final Stylized Facts Status",
        "",
        "## Included Cases",
        "- Fed Dec-to-Apr",
        "- Anthropic valuation",
        "",
        "## Excluded Case",
        "- Trump",
        "",
        "## Thesis-Ready Figures",
        "- Fed probability paths",
        "- Fed snapshot accuracy",
        "- Fed realized-rate context",
        "- Anthropic threshold probabilities",
        "- Anthropic core threshold probabilities",
        "",
        "## Thesis-Ready Notes",
        "- Fed final interpretation",
        "- Anthropic final interpretation",
        "- Stylized facts writing guide",
        "",
        "## Remaining Writing Task",
        "Write the stylized-facts interpretation directly into the thesis text and cite the figures and notes as the empirical basis.",
        "",
    ]
    status_path = notes_dir / "final_stylized_facts_status.md"
    status_path.write_text("\n".join(status_lines).strip() + "\n", encoding="utf-8")

    return {
        "fed": fed_path,
        "anthropic": anthropic_path,
        "guide": guide_path,
        "status": status_path,
    }


def _render_anthropic_figures(final_figures_dir: Path, anthropic_context: dict[str, Any]) -> dict[str, Path]:
    daily_rows = anthropic_context["daily_rows"]
    grouped: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for row in daily_rows:
        label = _anthropic_threshold_label(str(row.get("market_question", "")))
        dt = _parse_date(str(row.get("date", "")))
        prob = _safe_float(row.get("last_probability"))
        if dt is None or prob is None:
            continue
        grouped[label].append((datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc), prob))

    def _series(order: Iterable[str]) -> tuple[dict[str, list[tuple[datetime, float]]], list[str]]:
        series = {}
        labels = []
        for label in order:
            if label in grouped:
                series[label] = sorted(grouped[label], key=lambda item: item[0])
                labels.append(label)
        return series, labels

    full_series, full_labels = _series(ANTHROPIC_FULL_ORDER)
    core_series, core_labels = _series(ANTHROPIC_CORE_ORDER)

    full_path = final_figures_dir / "anthropic_threshold_probabilities"
    _render_series_chart(
        full_path,
        "Anthropic threshold probabilities",
        "Prediction-market threshold probabilities tied to an institutional private-market valuation reference.",
        full_series,
        "The chart shows threshold probabilities, not a direct Anthropic valuation. Nasdaq Private Market / SecondMarket should be treated as institutional reference data; Forge and Secondary Suite can be cited only as contextual secondary-market signals.",
        ANTHROPIC_COLORS,
        full_labels,
        "Date (UTC)",
        "Probability",
    )

    core_path = final_figures_dir / "anthropic_threshold_probabilities_core"
    _render_series_chart(
        core_path,
        "Anthropic threshold probabilities (core)",
        "Prediction-market threshold probabilities tied to an institutional private-market valuation reference.",
        core_series,
        "Core subset of thresholds chosen for readability; the full chart retains all nine recovered bands.",
        ANTHROPIC_COLORS,
        core_labels,
        "Date (UTC)",
        "Probability",
    )

    table_path = final_figures_dir / "anthropic_threshold_summary_table"
    table_rows = _anthropic_table_rows(anthropic_context)
    _render_table(
        table_path,
        "Anthropic threshold summary",
        ["Threshold", "First", "Latest", "Min", "Max", "Interpretive note"],
        table_rows,
        [160, 120, 120, 120, 120, 520],
        "The summary table complements the chart by showing how the probability distribution is ordered across valuation bands.",
    )

    return {
        "anthropic_threshold_probabilities": full_path.with_suffix(".png"),
        "anthropic_threshold_probabilities_core": core_path.with_suffix(".png"),
        "anthropic_threshold_summary_table": table_path.with_suffix(".png"),
    }


def _render_fed_figures(final_figures_dir: Path, fed_context: dict[str, Any]) -> dict[str, Path]:
    comparator_rows = fed_context["comparator_rows"]
    snapshot_rows = fed_context["snapshot_rows"]
    daily_rows = fed_context["daily_rows"]
    summary_rows = fed_context["summary_rows"]
    nyfed_rows = fed_context["nyfed_rows"]
    realized = _realized_labels(comparator_rows)
    by_meeting = _probability_rows_by_meeting(daily_rows)

    panels = []
    for meeting_month in FED_MEETINGS:
        if meeting_month not in by_meeting:
            continue
        panels.append({"meeting_month": meeting_month, "data": by_meeting[meeting_month], "realized_label": realized.get(meeting_month, "")})

    probability_path = final_figures_dir / "fed_dec_to_apr_probability_paths"
    _render_small_multiples(
        probability_path,
        "Fed decision probability paths",
        "Four exact FOMC meetings reconstructed from Polymarket trade-level fallback series.",
        panels,
        "Probabilities are reconstructed from Polymarket trade-level fallback series rather than standardized prices-history endpoints.",
        "Days to decision",
    )

    snapshot_path = final_figures_dir / "fed_dec_to_apr_snapshot_accuracy"
    _render_snapshot_accuracy(snapshot_path, comparator_rows, snapshot_rows)

    realized_rates_path = final_figures_dir / "fed_dec_to_apr_realized_rates"
    _render_realized_rates(realized_rates_path, comparator_rows, nyfed_rows)

    summary_table_path = final_figures_dir / "fed_decision_summary_table"
    table_rows = []
    summary_lookup = {str(row.get("meeting_month", "")).strip(): row for row in summary_rows}
    for row in comparator_rows:
        meeting_month = str(row.get("meeting_month", "")).strip()
        target = realized.get(meeting_month, "")
        prob = _final_snapshot_probability(snapshot_rows, meeting_month, target)
        table_rows.append(
            [
                FED_MEETING_LABELS.get(meeting_month, meeting_month),
                str(row.get("official_decision", "")),
                _format_pct(prob),
                str(summary_lookup.get(meeting_month, {}).get("source_endpoint_type", "trades_fallback")),
                str(summary_lookup.get(meeting_month, {}).get("caveat_methodologique", "")),
            ]
        )
    _render_table(
        summary_table_path,
        "Fed decision summary",
        ["Meeting", "Realized decision", "Final prob.", "Source type", "Caveat"],
        table_rows,
        [180, 260, 140, 170, 520],
        "The final realized-outcome probability is the final available pre-meeting snapshot probability for the realized outcome.",
    )

    return {
        "fed_probability_paths": probability_path.with_suffix(".png"),
        "fed_snapshot_accuracy": snapshot_path.with_suffix(".png"),
        "fed_realized_rates": realized_rates_path.with_suffix(".png"),
        "fed_decision_summary_table": summary_table_path.with_suffix(".png"),
    }


def run_final_thesis_delivery(base_dir: Path | str = ".") -> dict[str, Path]:
    base_dir = Path(base_dir).resolve()
    data_dir = base_dir / "Data" if (base_dir / "Data").exists() else base_dir
    analysis_dir = ensure_dir(data_dir / "analysis")
    final_figures_dir = ensure_dir(analysis_dir / "final_figures")

    fed_context = _load_fed_context(data_dir)
    anthropic_context = _load_anthropic_context(data_dir)

    fed_outputs = _render_fed_figures(final_figures_dir, fed_context)
    anthropic_outputs = _render_anthropic_figures(final_figures_dir, anthropic_context)
    note_outputs = _write_notes(data_dir, fed_context, anthropic_context)

    outputs: dict[str, Path] = {}
    outputs.update(fed_outputs)
    outputs.update(anthropic_outputs)
    outputs.update(note_outputs)
    return outputs
