"""Build thesis-ready Polymarket analysis outputs from existing local CSVs.

This module is intentionally conservative:
- it only consumes the already-available focused Polymarket CSVs
- it does not fetch new data
- it does not fabricate missing snapshot anchors
- it keeps Trump/Fed explicitly marked as trade-based fallback series
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from html import escape as html_escape
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFont

from .io_utils import ensure_dir, read_csv_rows, write_csv


CASE_SPECS: dict[str, dict[str, Any]] = {
    "trump_2024": {
        "source_csv": "polymarket_trump_2024_price_history_long.csv",
        "event_slug": "presidential-election-winner-2024",
        "event_url_slug": "presidential-election-winner-2024",
        "display_name": "Trump 2024",
        "method_note": "Trade-based fallback series; available window currently limited to election day.",
        "chart_filename": "trump_2024_probability",
        "chart_mode": "intraday",
        "outcome_filter": {"Donald Trump"},
        "default_label": "Donald Trump",
    },
    "fed_january": {
        "source_csv": "polymarket_fed_january_price_history_long.csv",
        "event_slug": "fed-decision-in-january",
        "event_url_slug": "fed-decision-in-january",
        "display_name": "Fed January",
        "method_note": "Trade-based fallback series; no CME FedWatch comparator included yet.",
        "chart_filename": "fed_january_probability",
        "chart_mode": "daily",
        "outcome_filter": {"Yes"},
        "default_label": "Yes",
    },
    "anthropic_valuation": {
        "source_csv": "polymarket_anthropic_price_history_long.csv",
        "event_slug": "will-anthropics-valuation-hit-by-june-30",
        "event_url_slug": "will-anthropics-valuation-hit-by-june-30",
        "display_name": "Anthropic valuation",
        "method_note": "Prices-history series; ongoing market; 925B, 950B, and 975B thresholds missing history and documented separately.",
        "chart_filename": "anthropic_valuation_probability",
        "chart_mode": "daily",
        "outcome_filter": {"Yes"},
        "default_label": "Yes",
    },
}

DAILY_COLUMNS = (
    "date",
    "research_case",
    "market_question",
    "outcome_name",
    "token_id",
    "first_probability",
    "last_probability",
    "mean_probability",
    "min_probability",
    "max_probability",
    "number_of_observations",
    "source_endpoint_type",
)

SNAPSHOT_COLUMNS = (
    "research_case",
    "snapshot_label",
    "target_date",
    "selected_timestamp",
    "selected_datetime_utc",
    "selected_date",
    "event_slug",
    "event_title",
    "market_slug",
    "market_question",
    "condition_id",
    "token_id",
    "outcome_name",
    "source_endpoint_type",
    "probability",
    "source_endpoint",
    "raw_file_path",
    "data_status",
    "final_outcome_0_1",
    "resolution_status",
    "notes",
)


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
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        return date.fromisoformat(value[:10])
    except Exception:
        return None


def _format_date(value: date | None) -> str:
    return value.isoformat() if value else ""


def _format_datetime_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _endpoint_type(source_endpoint: str) -> str:
    if "data-api.polymarket.com/trades" in source_endpoint:
        return "trades_fallback"
    return "prices_history"


def _load_latest_json(raw_dir: Path, event_slug: str) -> tuple[dict[str, Any], Path]:
    candidates = sorted(raw_dir.glob(f"*event_{event_slug}.json"))
    if not candidates:
        raise FileNotFoundError(f"No Polymarket event JSON found for {event_slug}")
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    payload = json.loads(latest.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        if not payload or not isinstance(payload[0], dict):
            raise ValueError(f"Unexpected Polymarket event JSON shape for {event_slug}")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected Polymarket event JSON shape for {event_slug}")
    return payload, latest


def _parse_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return [value.strip()]
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    return []


def _market_is_resolved(market: dict[str, Any], event_closed: bool) -> bool:
    return bool(
        event_closed
        or market.get("closed")
        or market.get("automaticallyResolved")
        or market.get("resolvedBy")
        or market.get("umaResolutionStatus")
    )


def _market_final_outcomes(market: dict[str, Any], event_closed: bool) -> dict[str, str]:
    if not _market_is_resolved(market, event_closed):
        return {}
    token_ids = _parse_json_list(
        market.get("clobTokenIds") or market.get("clob_token_ids") or market.get("tokenIds") or market.get("token_ids")
    )
    outcome_prices = _parse_json_list(market.get("outcomePrices"))
    final_map: dict[str, str] = {}
    for token_id, outcome_price in zip(token_ids, outcome_prices):
        value = _safe_float(outcome_price)
        if value is None:
            continue
        final_map[token_id] = "1" if value >= 0.5 else "0"
    return final_map


def _event_context(base_dir: Path, case_key: str) -> dict[str, Any]:
    spec = CASE_SPECS[case_key]
    raw_dir = base_dir / "pipeline_outputs" / "raw" / "polymarket"
    payload, raw_path = _load_latest_json(raw_dir, spec["event_url_slug"])
    markets = payload.get("markets") if isinstance(payload.get("markets"), list) else []
    event_closed = bool(payload.get("closed") or payload.get("ended"))
    event_date = _parse_date(str(payload.get("endDate") or payload.get("endDateIso") or ""))
    resolution_status = "resolved" if event_closed else "open"
    market_final_outcomes: dict[str, str] = {}
    for market in markets:
        if not isinstance(market, dict):
            continue
        market_final_outcomes.update(_market_final_outcomes(market, event_closed))
    return {
        "research_case": case_key,
        "event_slug": spec["event_slug"],
        "event_title": str(payload.get("title") or spec["display_name"]),
        "event_date": event_date,
        "event_closed": event_closed,
        "resolution_status": resolution_status,
        "market_final_outcomes": market_final_outcomes,
        "raw_event_path": raw_path,
        "payload": payload,
    }


def _load_case_rows(base_dir: Path, case_key: str) -> list[dict[str, Any]]:
    spec = CASE_SPECS[case_key]
    path = base_dir / "pipeline_outputs" / "cleaned" / spec["source_csv"]
    rows = read_csv_rows(path)
    out: list[dict[str, Any]] = []
    for row in rows:
        token_id = str(row.get("token_id", "")).strip()
        source_endpoint = str(row.get("source_endpoint", "")).strip()
        raw_file_path = str(row.get("raw_file_path", "")).strip()
        probability = _safe_float(row.get("probability"))
        timestamp = _safe_float(row.get("timestamp"))
        dt = _parse_datetime(str(row.get("datetime_utc", "")).strip())
        if dt is None and timestamp is not None:
            dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        if not token_id or not source_endpoint or not raw_file_path or probability is None or dt is None:
            continue
        out.append(
            {
                "research_case": case_key,
                "event_slug": str(row.get("event_slug", spec["event_slug"])).strip(),
                "event_title": str(row.get("event_title", spec["display_name"])).strip(),
                "market_slug": str(row.get("market_slug", "")).strip(),
                "market_question": str(row.get("market_question", "")).strip(),
                "condition_id": str(row.get("condition_id", "")).strip(),
                "token_id": token_id,
                "outcome_name": str(row.get("outcome_name", "")).strip(),
                "timestamp": int(timestamp) if timestamp is not None else 0,
                "datetime_utc": _format_datetime_utc(dt),
                "_dt": dt,
                "date": dt.date().isoformat(),
                "price": _safe_float(row.get("price")),
                "probability": probability,
                "volume": row.get("volume", ""),
                "liquidity": row.get("liquidity", ""),
                "source_category": str(row.get("source_category", "")).strip(),
                "source_name": str(row.get("source_name", "")).strip(),
                "platform": str(row.get("platform", "")).strip(),
                "source_endpoint": source_endpoint,
                "source_endpoint_type": _endpoint_type(source_endpoint),
                "raw_file_path": raw_file_path,
                "data_status": str(row.get("data_status", "")).strip(),
                "use_in_probability_analysis": str(row.get("use_in_probability_analysis", "")).strip(),
            }
        )
    return out


def _build_daily_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[
            (
                row["date"],
                row["research_case"],
                row["market_question"],
                row["outcome_name"],
                row["token_id"],
            )
        ].append(row)

    daily_rows: list[dict[str, Any]] = []
    for (day, research_case, market_question, outcome_name, token_id), bucket in sorted(buckets.items()):
        ordered = sorted(bucket, key=lambda row: row["_dt"])
        probs = [row["probability"] for row in ordered if row["probability"] is not None]
        if not probs:
            continue
        daily_rows.append(
            {
                "date": day,
                "research_case": research_case,
                "market_question": market_question,
                "outcome_name": outcome_name,
                "token_id": token_id,
                "first_probability": ordered[0]["probability"],
                "last_probability": ordered[-1]["probability"],
                "mean_probability": mean(probs),
                "min_probability": min(probs),
                "max_probability": max(probs),
                "number_of_observations": len(bucket),
                "source_endpoint_type": ordered[0]["source_endpoint_type"],
            }
        )
    return daily_rows


def _select_snapshot_row(rows: list[dict[str, Any]], target_date: date | None) -> tuple[dict[str, Any] | None, str]:
    if not rows:
        return None, "no_data"
    ordered = sorted(rows, key=lambda row: row["_dt"])
    if target_date is None:
        return ordered[-1], "no_target_date"
    before_or_on = [row for row in ordered if row["_dt"].date() <= target_date]
    if before_or_on:
        return before_or_on[-1], "before_or_on_target"
    return None, "no_observation_before_target"


def _build_snapshot_rows(rows: list[dict[str, Any]], contexts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[(row["research_case"], row["token_id"])].append(row)

    labels = ["first_available", "T-60", "T-30", "T-14", "T-7", "T-3", "T-1", "final_available"]
    offsets = {"T-60": 60, "T-30": 30, "T-14": 14, "T-7": 7, "T-3": 3, "T-1": 1}

    snapshot_rows: list[dict[str, Any]] = []
    for (research_case, token_id), bucket in sorted(buckets.items()):
        context = contexts[research_case]
        ordered = sorted(bucket, key=lambda row: row["_dt"])
        for label in labels:
            if label == "first_available":
                selected = ordered[0]
                selection_mode = "first_available"
                target_date = ""
            elif label == "final_available":
                selected = ordered[-1]
                selection_mode = "final_available"
                target_date = ""
            else:
                target = context["event_date"] - timedelta(days=offsets[label]) if context["event_date"] else None
                selected, selection_mode = _select_snapshot_row(bucket, target)
                target_date = _format_date(target)
            if selected is None:
                continue
            notes = [f"snapshot_selection={selection_mode}"]
            snapshot_rows.append(
                {
                    "research_case": research_case,
                    "snapshot_label": label,
                    "target_date": target_date,
                    "selected_timestamp": selected["timestamp"],
                    "selected_datetime_utc": selected["datetime_utc"],
                    "selected_date": selected["date"],
                    "event_slug": selected["event_slug"],
                    "event_title": selected["event_title"],
                    "market_slug": selected["market_slug"],
                    "market_question": selected["market_question"],
                    "condition_id": selected["condition_id"],
                    "token_id": token_id,
                    "outcome_name": selected["outcome_name"],
                    "source_endpoint_type": selected["source_endpoint_type"],
                    "probability": selected["probability"],
                    "source_endpoint": selected["source_endpoint"],
                    "raw_file_path": selected["raw_file_path"],
                    "data_status": selected["data_status"],
                    "final_outcome_0_1": context["market_final_outcomes"].get(token_id, ""),
                    "resolution_status": context["resolution_status"],
                    "notes": "; ".join(notes),
                }
            )
    return snapshot_rows


def _available_final_outcome(context: dict[str, Any], token_id: str) -> int | None:
    value = context["market_final_outcomes"].get(token_id, "")
    if value == "1":
        return 1
    if value == "0":
        return 0
    return None


def _trump_brier_score(rows: list[dict[str, Any]], context: dict[str, Any]) -> float | None:
    token_ids = sorted({row["token_id"] for row in rows if row["research_case"] == "trump_2024"})
    if len(token_ids) != 1:
        return None
    final = _available_final_outcome(context, token_ids[0])
    if final != 1:
        return None
    probs = [row["probability"] for row in rows if row["research_case"] == "trump_2024"]
    if not probs:
        return None
    return mean((prob - 1.0) ** 2 for prob in probs)


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


def _series_label(case_key: str, market_question: str, outcome_name: str, market_slug: str) -> str:
    if case_key == "trump_2024":
        return "Donald Trump"
    if case_key == "fed_january":
        text = market_question.lower()
        if "50+ bps" in text or "50 bps" in text:
            return "50+ bps cut"
        if "25 bps" in text and "decreas" in text:
            return "25 bps cut"
        if "no change" in text:
            return "No change"
        if "increases" in text:
            return "25+ bps hike"
    if case_key == "anthropic_valuation":
        return _anthropic_threshold_label(market_question)
    return market_question or outcome_name


def _load_plot_rows(case_key: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spec = CASE_SPECS[case_key]
    filtered = [row for row in rows if row["outcome_name"] in spec["outcome_filter"]]
    if case_key == "trump_2024":
        return filtered
    return filtered


class _ChartBackend:
    def __init__(self, kind: str, width: int, height: int):
        self.kind = kind
        self.width = width
        self.height = height
        self.parts: list[str] = []
        self.image = Image.new("RGB", (width, height), "white") if kind == "png" else None
        self.draw = ImageDraw.Draw(self.image) if self.image is not None else None

    def line(self, xy1: tuple[float, float], xy2: tuple[float, float], fill: str, width: int = 1) -> None:
        if self.kind == "png":
            assert self.draw is not None
            self.draw.line([xy1, xy2], fill=fill, width=width)
        else:
            self.parts.append(
                f'<line x1="{xy1[0]:.2f}" y1="{xy1[1]:.2f}" x2="{xy2[0]:.2f}" y2="{xy2[1]:.2f}" stroke="{fill}" stroke-width="{width}" />'
            )

    def rect(self, box: tuple[float, float, float, float], fill: str | None = None, outline: str | None = None, width: int = 1, rx: int = 0) -> None:
        x0, y0, x1, y1 = box
        if self.kind == "png":
            assert self.draw is not None
            self.draw.rounded_rectangle(box, radius=rx, fill=fill, outline=outline, width=width)
        else:
            attrs = [
                f'x="{x0:.2f}"',
                f'y="{y0:.2f}"',
                f'width="{(x1 - x0):.2f}"',
                f'height="{(y1 - y0):.2f}"',
            ]
            if rx:
                attrs.append(f'rx="{rx}"')
                attrs.append(f'ry="{rx}"')
            if fill:
                attrs.append(f'fill="{fill}"')
            else:
                attrs.append('fill="none"')
            if outline:
                attrs.append(f'stroke="{outline}"')
                attrs.append(f'stroke-width="{width}"')
            else:
                attrs.append('stroke="none"')
            self.parts.append(f'<rect {" ".join(attrs)} />')

    def text(self, xy: tuple[float, float], text: str, font: ImageFont.FreeTypeFont, fill: str = "#111111", anchor: str = "la") -> tuple[float, float, float, float]:
        x, y = xy
        if self.kind == "png":
            assert self.draw is not None
            bbox = self.draw.textbbox((x, y), text, font=font, anchor=anchor)
            self.draw.text((x, y), text, font=font, fill=fill, anchor=anchor)
            return bbox
        anchor_map = {"la": "start", "lt": "start", "ma": "middle", "mt": "middle", "ra": "end", "rt": "end"}
        baseline = "text-before-edge" if anchor.endswith("t") else "central" if anchor.endswith("m") else "alphabetic"
        font_size = getattr(font, "size", 12)
        self.parts.append(
            f'<text x="{x:.2f}" y="{y:.2f}" fill="{fill}" font-family="DejaVu Sans, Arial, sans-serif" font-size="{font_size}" text-anchor="{anchor_map.get(anchor, "start")}" dominant-baseline="{baseline}">{html_escape(text)}</text>'
        )
        # Return a rough bbox for callers that need it.
        width = max(1, int(len(text) * font_size * 0.55))
        height = font_size
        return (x, y, x + width, y + height)

    def multiline_text(self, xy: tuple[float, float], text: str, font: ImageFont.FreeTypeFont, fill: str = "#111111", spacing: int = 4) -> None:
        x, y = xy
        if self.kind == "png":
            assert self.draw is not None
            self.draw.multiline_text((x, y), text, font=font, fill=fill, spacing=spacing)
        else:
            lines = text.splitlines()
            line_height = font.size + spacing
            for idx, line in enumerate(lines):
                self.text((x, y + idx * line_height), line, font=font, fill=fill, anchor="la")

    def polygon(self, points: list[tuple[float, float]], fill: str | None = None, outline: str | None = None, width: int = 1) -> None:
        if self.kind == "png":
            assert self.draw is not None
            self.draw.polygon(points, fill=fill, outline=outline)
            if outline and len(points) > 1:
                self.draw.line(points + [points[0]], fill=outline, width=width)
        else:
            point_str = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
            attrs = [f'points="{point_str}"']
            if fill:
                attrs.append(f'fill="{fill}"')
            else:
                attrs.append('fill="none"')
            if outline:
                attrs.append(f'stroke="{outline}"')
                attrs.append(f'stroke-width="{width}"')
            else:
                attrs.append('stroke="none"')
            self.parts.append(f'<polygon {" ".join(attrs)} />')

    def circle(self, center: tuple[float, float], radius: float, fill: str, outline: str | None = None, width: int = 1) -> None:
        x, y = center
        if self.kind == "png":
            assert self.draw is not None
            self.draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill, outline=outline, width=width)
        else:
            attrs = [
                f'cx="{x:.2f}"',
                f'cy="{y:.2f}"',
                f'r="{radius:.2f}"',
                f'fill="{fill}"',
            ]
            if outline:
                attrs.append(f'stroke="{outline}"')
                attrs.append(f'stroke-width="{width}"')
            self.parts.append(f'<circle {" ".join(attrs)} />')

    def save(self, path: Path) -> None:
        ensure_dir(path.parent)
        if self.kind == "png":
            assert self.image is not None
            self.image.save(path)
        else:
            svg = "\n".join(
                [
                    '<?xml version="1.0" encoding="UTF-8"?>',
                    f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" height="{self.height}" viewBox="0 0 {self.width} {self.height}">',
                    *self.parts,
                    "</svg>",
                ]
            )
            path.write_text(svg, encoding="utf-8")


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = []
    if bold:
        candidates.extend(
            [
                "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                "/System/Library/Fonts/Supplemental/Helvetica Bold.ttf",
                "/Library/Fonts/Arial Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ]
        )
    else:
        candidates.extend(
            [
                "/System/Library/Fonts/Supplemental/Arial.ttf",
                "/System/Library/Fonts/Supplemental/Helvetica.ttf",
                "/Library/Fonts/Arial.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]
        )
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            try:
                return ImageFont.truetype(str(path), size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int, backend: _ChartBackend) -> list[str]:
    words = text.split()
    if not words:
        return [text]
    lines: list[str] = []
    current = words[0]
    font_size = getattr(font, "size", 12)
    for word in words[1:]:
        candidate = f"{current} {word}"
        if backend.kind == "png":
            assert backend.draw is not None
            bbox = backend.draw.textbbox((0, 0), candidate, font=font)
            width = bbox[2] - bbox[0]
        else:
            width = len(candidate) * font_size * 0.55
        if width <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _draw_axes(backend: _ChartBackend, left: int, top: int, right: int, bottom: int) -> None:
    grid_color = "#e6e6e6"
    axis_color = "#2e2e2e"
    for tick in [0.0, 0.25, 0.5, 0.75, 1.0]:
        y = bottom - (bottom - top) * tick
        backend.line((left, y), (right, y), grid_color, 1)
    backend.line((left, top), (left, bottom), axis_color, 2)
    backend.line((left, bottom), (right, bottom), axis_color, 2)


def _draw_series_chart(
    output_base: Path,
    title: str,
    x_label: str,
    series_data: dict[str, list[tuple[datetime, float]]],
    note_lines: list[str],
    color_map: dict[str, str],
    legend_order: list[str],
    x_tick_formatter: Any = None,
    x_bounds: tuple[datetime, datetime] | None = None,
) -> None:
    width, height = 1500, 900
    left, top = 110, 95
    right, bottom = 1110, 675
    legend_left = 1160
    note_top = 705
    backend_png = _ChartBackend("png", width, height)
    backend_svg = _ChartBackend("svg", width, height)
    fonts = {
        "title": _load_font(30, bold=True),
        "body": _load_font(20),
        "small": _load_font(17),
        "tiny": _load_font(15),
    }

    for backend in (backend_png, backend_svg):
        backend.rect((0, 0, width, height), fill="white", outline=None)
        backend.text((left, 26), title, fonts["title"], fill="#0b1f3a", anchor="la")
        backend.text((left, 62), "Probability", fonts["body"], fill="#333333", anchor="la")
        backend.text(((left + right) / 2, height - 32), x_label, fonts["body"], fill="#333333", anchor="ma")
        _draw_axes(backend, left, top, right, bottom)

        all_points = [(x, y) for series in series_data.values() for x, y in series]
        if not all_points:
            continue
        if x_bounds is None:
            x_values = [x for x, _ in all_points]
            x_min, x_max = min(x_values), max(x_values)
        else:
            x_min, x_max = x_bounds
        if x_min == x_max:
            x_max = x_max + timedelta(days=1)

        def x_to_px(x: datetime) -> float:
            total = (x_max - x_min).total_seconds() or 1.0
            return left + ((x - x_min).total_seconds() / total) * (right - left)

        def y_to_px(y: float) -> float:
            y = max(0.0, min(1.0, y))
            return bottom - y * (bottom - top)

        # Y-axis tick labels.
        for tick in [0.0, 0.25, 0.5, 0.75, 1.0]:
            y = y_to_px(tick)
            label = f"{tick:.2f}".rstrip("0").rstrip(".")
            backend.text((left - 14, y), label, fonts["tiny"], fill="#666666", anchor="ra")

        # X-axis tick labels.
        if x_tick_formatter is None:
            total_days = max((x_max - x_min).total_seconds() / 86400.0, 1.0)
            desired = 5 if total_days > 1 else 4
            ticks = [x_min + (x_max - x_min) * i / max(desired - 1, 1) for i in range(desired)]
            for tick in ticks:
                x = x_to_px(tick)
                label = tick.strftime("%Y-%m-%d")
                backend.text((x, bottom + 14), label, fonts["tiny"], fill="#666666", anchor="ma")
        else:
            ticks = x_tick_formatter(x_min, x_max)
            for tick_dt, label in ticks:
                x = x_to_px(tick_dt)
                backend.text((x, bottom + 14), label, fonts["tiny"], fill="#666666", anchor="ma")

        # Series lines and points.
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

        # Legend.
        legend_font = fonts["small"]
        legend_title_font = fonts["body"]
        backend.text((legend_left, top), "Series", legend_title_font, fill="#0b1f3a", anchor="la")
        legend_y = top + 40
        for label in legend_order:
            if label not in series_data:
                continue
            color = color_map.get(label, "#0b1f3a")
            backend.line((legend_left, legend_y + 10), (legend_left + 30, legend_y + 10), color, 4)
            backend.circle((legend_left + 15, legend_y + 10), 4, fill=color, outline="white", width=1)
            backend.text((legend_left + 42, legend_y + 10), label, legend_font, fill="#333333", anchor="la")
            legend_y += 30

        # Notes.
        note_box = (left, note_top, right, height - 40)
        backend.rect(note_box, fill="white", outline="#cccccc", width=1, rx=8)
        wrapped_lines: list[str] = []
        for note in note_lines:
            wrapped_lines.extend(_wrap_text(note, fonts["small"], right - left - 30, backend))
        backend.multiline_text((left + 14, note_top + 12), "\n".join(wrapped_lines), fonts["small"], fill="#333333", spacing=5)

    backend_png.save(output_base.with_suffix(".png"))
    backend_svg.save(output_base.with_suffix(".svg"))


def _plot_trump(case_rows: list[dict[str, Any]], output_base: Path, note: str, brier_score: float | None) -> None:
    ordered = sorted(case_rows, key=lambda row: row["_dt"])
    series = {
        "Donald Trump": [(row["_dt"], row["probability"]) for row in ordered],
    }
    note_lines = [note]
    if brier_score is not None:
        note_lines.append(f"Brier score on available observations: {brier_score:.4f}")

    def x_tick_formatter(x_min: datetime, x_max: datetime) -> list[tuple[datetime, str]]:
        if x_min == x_max:
            return [(x_min, x_min.strftime("%H:%M"))]
        total = (x_max - x_min).total_seconds()
        steps = 5 if total > 4 * 3600 else 4
        return [
            (x_min + (x_max - x_min) * i / max(steps - 1, 1), (x_min + (x_max - x_min) * i / max(steps - 1, 1)).strftime("%H:%M"))
            for i in range(steps)
        ]

    _draw_series_chart(
        output_base,
        "Trump 2024: election-day transaction-implied probability",
        "UTC time",
        series,
        note_lines,
        {"Donald Trump": "#0b1f3a"},
        ["Donald Trump"],
        x_tick_formatter=x_tick_formatter,
        x_bounds=(ordered[0]["_dt"], ordered[-1]["_dt"]),
    )


def _plot_daily_lines(
    case_key: str,
    daily_rows: list[dict[str, Any]],
    output_base: Path,
    note: str,
    line_order: list[str],
    color_map: dict[str, str],
) -> None:
    series_data: dict[str, list[tuple[datetime, float]]] = {}
    for label in line_order:
        series_rows = [row for row in daily_rows if row.get("_label") == label]
        if not series_rows:
            continue
        series_rows = sorted(series_rows, key=lambda row: row["date"])
        series_data[label] = [
            (datetime.fromisoformat(row["date"]).replace(tzinfo=timezone.utc), float(row["last_probability"]))
            for row in series_rows
        ]
    if not series_data:
        return
    x_bounds = (
        min(point[0] for series in series_data.values() for point in series),
        max(point[0] for series in series_data.values() for point in series),
    )
    _draw_series_chart(
        output_base,
        f"{CASE_SPECS[case_key]['display_name']}: daily last probability",
        "Date (UTC)",
        series_data,
        [note],
        color_map,
        line_order,
        x_bounds=x_bounds,
    )


def _prepare_daily_plot_rows(case_key: str, daily_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spec = CASE_SPECS[case_key]
    case_rows = [row for row in daily_rows if row["research_case"] == case_key]
    if case_key == "anthropic_valuation":
        selected: list[dict[str, Any]] = []
        for row in case_rows:
            if row["outcome_name"] == "Yes":
                row = dict(row)
                row["_label"] = _anthropic_threshold_label(row["market_question"])
                selected.append(row)
        return selected
    if case_key == "fed_january":
        selected = []
        for row in case_rows:
            if row["outcome_name"] == "Yes":
                row = dict(row)
                row["_label"] = _series_label(case_key, row["market_question"], row["outcome_name"], "")
                selected.append(row)
        return selected
    # Trump is plotted from raw intraday observations, not the daily aggregation.
    return case_rows


def _build_memo(base_dir: Path, contexts: dict[str, dict[str, Any]], daily_rows: list[dict[str, Any]], snapshot_rows: list[dict[str, Any]], trump_brier: float | None) -> str:
    lines: list[str] = [
        "# Current Status and Next Steps",
        "",
        "## Where We Are",
        "The thesis now has three local Polymarket case datasets built from the existing focused CSVs only. No new benchmark sources were fetched for this step.",
        "",
        "## Data Available",
    ]
    for case_key in ("trump_2024", "fed_january", "anthropic_valuation"):
        spec = CASE_SPECS[case_key]
        rows = [row for row in daily_rows if row["research_case"] == case_key]
        token_ids = sorted({row["token_id"] for row in rows})
        outcomes = sorted({row["outcome_name"] for row in rows})
        dates = sorted({row["date"] for row in rows})
        lines.append(
            f"- {case_key}: {len(rows)} daily rows, {len(token_ids)} token IDs, outcomes={', '.join(outcomes)}, dates={dates[0] if dates else 'n/a'} to {dates[-1] if dates else 'n/a'}"
        )
    lines.extend(
        [
            "",
            "## Thesis Readiness",
            "- Trump 2024: ready only as an election-day transaction-implied series, with a hard caveat that it is not a long-window convergence or accuracy figure.",
            "- Fed January: ready as a Polymarket-implied expectations series, but still without a CME FedWatch comparator.",
            "- Anthropic valuation: ready as the main actionability/private-market threshold case, with the missing 925B, 950B, and 975B histories documented separately.",
            "",
            "## Missing External Sources",
            "- CME FedWatch or a comparable Fed benchmark, if you want an external comparison layer.",
            "- Any later recovered Polymarket price_history for Trump if you want a proper long-window convergence analysis.",
            "- Optional later-stage private-market reference annotations for Anthropic, such as Nasdaq Private Market / SecondMarket / Forge / Secondary Suite signals.",
            "",
            "## Anthropic Framing",
            "Polymarket does not directly value Anthropic. It prices the probability that Nasdaq Private Market / SecondMarket-linked valuation thresholds are reached. That makes the series useful for studying actionability: an investor can compare the market-implied threshold probabilities against their own entry valuation and decision threshold, rather than asking only whether the market is 'right'.",
            "",
            "## Notes",
        ]
    )
    if trump_brier is not None:
        lines.append(f"- Trump Brier score over available observations: {trump_brier:.4f}")
    lines.append("- Trump should not be oversold as a convergence or accuracy panel until a longer price-history window is recovered.")
    lines.append("- Figures were built from the locally available Polymarket CSVs only.")
    lines.append("")
    return "\n".join(lines).strip() + "\n"


def run_thesis_analysis(base_dir: Path | str = ".") -> dict[str, Path]:
    base_dir = Path(base_dir).resolve()
    analysis_dir = ensure_dir(base_dir / "analysis")
    daily_dir = ensure_dir(analysis_dir / "daily")
    snapshot_dir = ensure_dir(analysis_dir / "snapshots")
    figures_dir = ensure_dir(analysis_dir / "figures")
    notes_dir = ensure_dir(analysis_dir / "notes")

    contexts = {case_key: _event_context(base_dir, case_key) for case_key in ("trump_2024", "fed_january", "anthropic_valuation")}
    raw_rows: list[dict[str, Any]] = []
    for case_key in ("trump_2024", "fed_january", "anthropic_valuation"):
        raw_rows.extend(_load_case_rows(base_dir, case_key))

    daily_rows = _build_daily_rows(raw_rows)
    daily_paths: dict[str, Path] = {}
    for case_key in ("trump_2024", "fed_january", "anthropic_valuation"):
        path = daily_dir / f"{case_key}_daily.csv"
        case_rows = [
            {k: row[k] for k in DAILY_COLUMNS}
            for row in daily_rows
            if row["research_case"] == case_key
        ]
        write_csv(path, DAILY_COLUMNS, case_rows)
        daily_paths[case_key] = path

    snapshot_rows = _build_snapshot_rows(raw_rows, contexts)
    snapshot_paths: dict[str, Path] = {}
    for case_key in ("trump_2024", "fed_january", "anthropic_valuation"):
        path = snapshot_dir / f"{case_key}_snapshots.csv"
        case_rows = [
            {k: row.get(k, "") for k in SNAPSHOT_COLUMNS}
            for row in snapshot_rows
            if row["research_case"] == case_key
        ]
        write_csv(path, SNAPSHOT_COLUMNS, case_rows)
        snapshot_paths[case_key] = path

    trump_context = contexts["trump_2024"]
    trump_raw = [row for row in raw_rows if row["research_case"] == "trump_2024"]
    trump_brier = _trump_brier_score(trump_raw, trump_context)

    # Figure 1: Trump intraday series.
    trump_fig_path = figures_dir / CASE_SPECS["trump_2024"]["chart_filename"]
    _plot_trump(trump_raw, trump_fig_path, CASE_SPECS["trump_2024"]["method_note"], trump_brier)

    # Figure 2: Fed daily last probability by outcome.
    fed_plot_rows = _prepare_daily_plot_rows("fed_january", daily_rows)
    fed_fig_path = figures_dir / CASE_SPECS["fed_january"]["chart_filename"]
    fed_color_map = {
        "50+ bps cut": "#0b1f3a",
        "25 bps cut": "#4c5563",
        "No change": "#8a8f98",
        "25+ bps hike": "#b0b5bd",
    }
    _plot_daily_lines(
        "fed_january",
        fed_plot_rows,
        fed_fig_path,
        CASE_SPECS["fed_january"]["method_note"],
        ["50+ bps cut", "25 bps cut", "No change", "25+ bps hike"],
        fed_color_map,
    )

    # Figure 3: Anthropic threshold series.
    anthropic_plot_rows = _prepare_daily_plot_rows("anthropic_valuation", daily_rows)
    anthropic_fig_path = figures_dir / CASE_SPECS["anthropic_valuation"]["chart_filename"]
    anthropic_color_map = {
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
    _plot_daily_lines(
        "anthropic_valuation",
        anthropic_plot_rows,
        anthropic_fig_path,
        CASE_SPECS["anthropic_valuation"]["method_note"],
        ["LOW 750B", "LOW 800B", "LOW 850B", "LOW 875B", "HIGH 1.0T", "HIGH 1.1T", "HIGH 1.25T", "HIGH 1.5T", "HIGH 1.75T"],
        anthropic_color_map,
    )

    memo_path = notes_dir / "current_status_and_next_steps.md"
    memo_path.write_text(_build_memo(base_dir, contexts, daily_rows, snapshot_rows, trump_brier), encoding="utf-8")

    return {
        "trump_daily": daily_paths["trump_2024"],
        "fed_daily": daily_paths["fed_january"],
        "anthropic_daily": daily_paths["anthropic_valuation"],
        "trump_snapshots": snapshot_paths["trump_2024"],
        "fed_snapshots": snapshot_paths["fed_january"],
        "anthropic_snapshots": snapshot_paths["anthropic_valuation"],
        "trump_figure": trump_fig_path.with_suffix(".png"),
        "fed_figure": fed_fig_path.with_suffix(".png"),
        "anthropic_figure": anthropic_fig_path.with_suffix(".png"),
        "memo": memo_path,
    }
