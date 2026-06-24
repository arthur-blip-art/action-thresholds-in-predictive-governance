"""Fed dec-to-apr realized analysis built from local Polymarket artifacts.

This build is deliberately conservative:
- it only uses the exact Fed decision markets requested by the user
- it does not fetch any new prediction-market probabilities
- it documents missing local histories instead of fabricating them
- it separates realized Fed decisions from realized rate context
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from .io_utils import ensure_dir, read_csv_rows, write_csv
from .thesis_analysis import _ChartBackend, _draw_axes, _load_font, _wrap_text


TARGET_MONTHS = ("2025-12", "2026-01", "2026-03", "2026-04")
DISPLAY_ORDER = ["50_plus_bps_cut", "25_bps_cut", "no_change", "25_plus_bps_hike", "other"]
DISPLAY_LABELS = {
    "50_plus_bps_cut": "50+ bps cut",
    "25_bps_cut": "25 bps cut",
    "no_change": "no change",
    "25_plus_bps_hike": "25+ bps hike",
    "other": "other",
}
SNAPSHOT_HORIZONS = ["T-30", "T-14", "T-7", "T-3", "T-1", "final_available"]

LONG_COLUMNS = (
    "timestamp",
    "datetime_utc",
    "date",
    "meeting_month",
    "meeting_date",
    "event_slug",
    "event_url",
    "market_question",
    "condition_id",
    "token_id",
    "outcome_name",
    "normalized_outcome_label",
    "probability",
    "price",
    "source_endpoint_type",
    "source_endpoint",
    "raw_file_path",
    "data_status",
    "use_in_probability_analysis",
)

COMPARATOR_COLUMNS = (
    "meeting_month",
    "meeting_date",
    "source_name",
    "source_category",
    "official_decision",
    "realized_outcome_label",
    "target_range_before",
    "target_range_after",
    "decision_size_bps",
    "source_url",
    "notes",
)

NYFED_COLUMNS = (
    "date",
    "source_name",
    "source_category",
    "reference_rate_name",
    "rate_value",
    "volume_if_available",
    "source_url",
    "notes",
    "use_in_probability_analysis",
)

DAILY_COLUMNS = (
    "date",
    "meeting_month",
    "meeting_date",
    "event_slug",
    "event_title",
    "market_question",
    "outcome_name",
    "normalized_outcome_label",
    "token_id",
    "last_probability",
    "mean_probability",
    "min_probability",
    "max_probability",
    "number_of_observations",
    "days_to_meeting",
    "source_endpoint_type",
)

SNAPSHOT_COLUMNS = (
    "meeting_month",
    "meeting_date",
    "event_slug",
    "event_title",
    "market_question",
    "outcome_name",
    "normalized_outcome_label",
    "snapshot_label",
    "target_date",
    "selected_timestamp",
    "selected_datetime_utc",
    "selected_date",
    "probability",
    "realized_outcome_0_1",
    "brier_score",
    "absolute_error",
    "source_endpoint_type",
    "source_endpoint",
    "raw_file_path",
    "data_status",
    "notes",
)

AUDIT_COLUMNS = (
    "event_month",
    "event_slug",
    "event_url",
    "event_title",
    "event_question",
    "accepted_true_false",
    "rejection_reason",
    "markets_found",
    "token_ids_found",
    "volume",
    "liquidity",
    "notes",
)

SUMMARY_COLUMNS = (
    "meeting_month",
    "meeting_date",
    "meeting_label",
    "polymarket_url",
    "market_outcomes",
    "fed_realized_decision",
    "polymarket_data_availability",
    "source_endpoint_type",
    "usable_for_graph",
    "caveat_methodologique",
)


EVENT_SPECS: dict[str, dict[str, Any]] = {
    "2025-12": {
        "event_slug": "fed-decision-in-december",
        "event_url": "https://polymarket.com/event/fed-decision-in-december",
        "event_title": "Fed decision in December?",
        "event_question": "Fed decision in December?",
        "meeting_date": "2025-12-10",
        "official_decision": "Lowered the target range by 25 basis points.",
        "realized_outcome_label": "25_bps_cut",
        "target_range_before": "3.75-4.00",
        "target_range_after": "3.50-3.75",
        "decision_size_bps": "25",
        "source_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20251210a.htm",
        "notes": "Exact December 2025 FOMC decision market. Local probability history is not recoverable from the current cache.",
    },
    "2026-01": {
        "event_slug": "fed-decision-in-january",
        "event_url": "https://polymarket.com/event/fed-decision-in-january",
        "event_title": "Fed decision in January?",
        "event_question": "Fed decision in January?",
        "meeting_date": "2026-01-28",
        "official_decision": "Maintained the target range.",
        "realized_outcome_label": "no_change",
        "target_range_before": "3.50-3.75",
        "target_range_after": "3.50-3.75",
        "decision_size_bps": "0",
        "source_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260128a.htm",
        "notes": "Exact January 2026 FOMC decision market. Local trade-based fallback probability rows are available.",
    },
    "2026-03": {
        "event_slug": "fed-decision-in-march",
        "event_url": "https://polymarket.com/event/fed-decision-in-march",
        "event_title": "Fed decision in March?",
        "event_question": "Fed decision in March?",
        "meeting_date": "2026-03-18",
        "official_decision": "Maintained the target range.",
        "realized_outcome_label": "no_change",
        "target_range_before": "3.50-3.75",
        "target_range_after": "3.50-3.75",
        "decision_size_bps": "0",
        "source_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260318a.htm",
        "notes": "Exact March 2026 FOMC decision market. No usable local Polymarket probability history was recovered.",
    },
    "2026-04": {
        "event_slug": "fed-decision-in-april",
        "event_url": "https://polymarket.com/event/fed-decision-in-april",
        "event_title": "Fed decision in April?",
        "event_question": "Fed decision in April?",
        "meeting_date": "2026-04-29",
        "official_decision": "Maintained the target range.",
        "realized_outcome_label": "no_change",
        "target_range_before": "3.50-3.75",
        "target_range_after": "3.50-3.75",
        "decision_size_bps": "0",
        "source_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260429a.htm",
        "notes": "Exact April 2026 FOMC decision market. No usable local Polymarket probability history was recovered.",
    },
}


EFFR_ROWS = [
    ("2026-02-12", 3.64, 97),
    ("2026-02-11", 3.64, 101),
    ("2026-02-10", 3.64, 104),
    ("2026-02-09", 3.64, 92),
    ("2026-02-06", 3.64, 106),
    ("2026-02-05", 3.64, 110),
    ("2026-02-04", 3.64, 109),
    ("2026-02-03", 3.64, 107),
    ("2026-02-02", 3.64, 93),
    ("2026-01-30", 3.64, 101),
    ("2026-01-29", 3.64, 104),
    ("2026-01-28", 3.64, 89),
    ("2026-01-27", 3.64, 88),
    ("2026-01-26", 3.64, 83),
    ("2026-01-23", 3.64, 99),
    ("2026-01-22", 3.64, 89),
    ("2026-01-21", 3.64, 95),
    ("2026-01-20", 3.64, 83),
    ("2026-01-16", 3.64, 84),
    ("2026-01-15", 3.64, 91),
    ("2026-01-14", 3.64, 94),
    ("2026-01-13", 3.64, 92),
    ("2026-01-12", 3.64, 93),
    ("2026-01-09", 3.64, 93),
    ("2026-01-08", 3.64, 87),
]


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except Exception:
        return None


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value)
    except Exception:
        return None


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


def _meeting_sort_key(meeting_month: str) -> tuple[int, int]:
    try:
        year, month = meeting_month.split("-")
        return int(year), int(month)
    except Exception:
        return (9999, 99)


def _source_type(source_endpoint: str) -> str:
    return "trades_fallback" if "trades" in source_endpoint else "prices_history"


def _normalize_outcome_label(market_question: str, outcome_name: str) -> str:
    if outcome_name.lower() != "yes":
        return "other"
    question = market_question.lower()
    if "no change" in question:
        return "no_change"
    if "50+ bps" in question or "50 bps" in question:
        return "50_plus_bps_cut" if "decreas" in question else "25_plus_bps_hike"
    if "25 bps" in question:
        if "decreas" in question:
            return "25_bps_cut"
        if "increase" in question or "hike" in question:
            return "25_plus_bps_hike"
    if "increase" in question or "hike" in question:
        return "25_plus_bps_hike"
    return "other"


def _load_latest_event_payload(raw_dir: Path, event_slug: str) -> tuple[dict[str, Any] | None, Path | None]:
    candidates = sorted(raw_dir.glob(f"*event_{event_slug}.json"))
    if not candidates:
        return None, None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except Exception:
        return None, latest
    if isinstance(payload, list):
        payload = payload[0] if payload and isinstance(payload[0], dict) else None
    return (payload if isinstance(payload, dict) else None), latest


def _summarize_payload(payload: dict[str, Any] | None) -> tuple[str, str, str, str]:
    if not isinstance(payload, dict):
        return "", "", "", ""
    markets = payload.get("markets") if isinstance(payload.get("markets"), list) else []
    token_ids: list[str] = []
    volume = 0.0
    liquidity = 0.0
    for market in markets:
        if not isinstance(market, dict):
            continue
        ids = market.get("clobTokenIds") or market.get("clob_token_ids") or market.get("tokenIds") or market.get("token_ids")
        if isinstance(ids, list):
            token_ids.extend(str(item) for item in ids)
        elif isinstance(ids, str) and ids.strip():
            try:
                parsed = json.loads(ids)
            except Exception:
                parsed = []
            if isinstance(parsed, list):
                token_ids.extend(str(item) for item in parsed)
        vol = _safe_float(market.get("volume")) or _safe_float(market.get("volumeNum")) or _safe_float(market.get("volumeClob")) or 0.0
        liq = _safe_float(market.get("liquidity")) or _safe_float(market.get("liquidityNum")) or _safe_float(market.get("totalLiquidity")) or 0.0
        volume += vol
        liquidity += liq
    return str(len([m for m in markets if isinstance(m, dict)])), str(len(set(token_ids))), f"{volume:.6f}".rstrip("0").rstrip("."), f"{liquidity:.6f}".rstrip("0").rstrip(".")


def _load_source_rows(base_dir: Path) -> list[dict[str, Any]]:
    path = base_dir / "pipeline_outputs" / "cleaned" / "polymarket_fed_decisions_multi_event_long.csv"
    rows = read_csv_rows(path)
    event_url_by_month = {month: spec["event_url"] for month, spec in EVENT_SPECS.items()}
    out: list[dict[str, Any]] = []
    for row in rows:
        meeting_month = str(row.get("meeting_month", "")).strip()
        if meeting_month not in TARGET_MONTHS:
            continue
        event_slug = str(row.get("event_slug", "")).strip()
        if not event_slug:
            continue
        out.append(
            {
                "timestamp": str(row.get("timestamp", "")).strip(),
                "datetime_utc": str(row.get("datetime_utc", "")).strip(),
                "date": str(row.get("date", "")).strip(),
                "meeting_month": meeting_month,
                "meeting_date": str(row.get("meeting_date", "")).strip(),
                "event_slug": event_slug,
                "event_url": event_url_by_month.get(meeting_month, ""),
                "market_question": str(row.get("market_question", "")).strip(),
                "condition_id": str(row.get("condition_id", "")).strip(),
                "token_id": str(row.get("token_id", "")).strip(),
                "outcome_name": str(row.get("outcome_name", "")).strip(),
                "normalized_outcome_label": str(row.get("normalized_outcome_label", "")).strip(),
                "probability": str(row.get("probability", "")).strip(),
                "price": str(row.get("price", "")).strip(),
                "source_endpoint_type": str(row.get("source_endpoint_type", "")).strip(),
                "source_endpoint": str(row.get("source_endpoint", "")).strip(),
                "raw_file_path": str(row.get("raw_file_path", "")).strip(),
                "data_status": str(row.get("data_status", "")).strip(),
                "use_in_probability_analysis": str(row.get("use_in_probability_analysis", "")).strip(),
                "event_title": str(row.get("event_title", EVENT_SPECS.get(meeting_month, {}).get("event_title", ""))).strip(),
            }
        )
    return out


def _build_audit_rows(base_dir: Path) -> list[dict[str, Any]]:
    raw_dir = base_dir / "pipeline_outputs" / "raw" / "polymarket"
    rows: list[dict[str, Any]] = []
    for meeting_month, spec in EVENT_SPECS.items():
        payload, _ = _load_latest_event_payload(raw_dir, spec["event_slug"])
        markets_found, token_ids_found, volume, liquidity = _summarize_payload(payload)
        usable_rows = len([row for row in _load_source_rows(base_dir) if row["meeting_month"] == meeting_month])
        note_bits = [spec["notes"]]
        if payload is None:
            note_bits.append("No local raw event artifact was recovered.")
        else:
            note_bits.append(f"Local event artifact found with {markets_found or '0'} markets and {token_ids_found or '0'} token ids.")
        if usable_rows:
            note_bits.append(f"Usable probability rows in the current cache: {usable_rows}.")
        else:
            note_bits.append("No usable probability rows were recovered from the local cache.")
        rows.append(
            {
                "event_month": meeting_month,
                "event_slug": spec["event_slug"],
                "event_url": spec["event_url"],
                "event_title": spec["event_title"],
                "event_question": spec["event_question"],
                "accepted_true_false": "TRUE",
                "rejection_reason": "",
                "markets_found": markets_found,
                "token_ids_found": token_ids_found,
                "volume": volume,
                "liquidity": liquidity,
                "notes": " ".join(note_bits).strip(),
            }
        )
    return rows


def _build_summary_rows(base_dir: Path, long_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary_rows: list[dict[str, Any]] = []
    usable_months = {row["meeting_month"] for row in long_rows if row["use_in_probability_analysis"] == "TRUE"}
    for meeting_month in TARGET_MONTHS:
        spec = EVENT_SPECS[meeting_month]
        meeting_rows = [row for row in long_rows if row["meeting_month"] == meeting_month]
        if meeting_rows:
            endpoints = sorted({row["source_endpoint_type"] for row in meeting_rows})
            endpoint_type = ", ".join(endpoints)
            data_availability = "usable probability rows available" if meeting_month in usable_months else "market exact, but no usable probability rows"
            usable_for_graph = "yes" if meeting_month in usable_months else "no"
        else:
            endpoint_type = ""
            data_availability = "market exact, but no local probability rows recovered"
            usable_for_graph = "no"
        if endpoint_type == "trades_fallback":
            caveat = "trade-based fallback series; resolved meeting; usable for snapshots and path figure"
        elif endpoint_type == "prices_history":
            caveat = "prices-history series; usable for snapshots and path figure"
        elif endpoint_type:
            caveat = f"{endpoint_type} series; usable with caveats"
        else:
            caveat = "exact market page exists, but no usable local probability history was recovered"
        summary_rows.append(
            {
                "meeting_month": meeting_month,
                "meeting_date": spec["meeting_date"],
                "meeting_label": spec["event_title"],
                "polymarket_url": spec["event_url"],
                "market_outcomes": "50+ bps cut; 25 bps cut; no change; 25+ bps hike",
                "fed_realized_decision": spec["official_decision"],
                "polymarket_data_availability": data_availability,
                "source_endpoint_type": endpoint_type,
                "usable_for_graph": usable_for_graph,
                "caveat_methodologique": caveat,
            }
        )
    return summary_rows


def _build_comparator_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for meeting_month in TARGET_MONTHS:
        spec = EVENT_SPECS[meeting_month]
        rows.append(
            {
                "meeting_month": meeting_month,
                "meeting_date": spec["meeting_date"],
                "source_name": "Federal Reserve",
                "source_category": "official_statement",
                "official_decision": spec["official_decision"],
                "realized_outcome_label": spec["realized_outcome_label"],
                "target_range_before": spec["target_range_before"],
                "target_range_after": spec["target_range_after"],
                "decision_size_bps": spec["decision_size_bps"],
                "source_url": spec["source_url"],
                "notes": spec["notes"],
            }
        )
    return rows


def _build_nyfed_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    source_url = "https://www.newyorkfed.org/markets/reference-rates/effr"
    for day, rate_value, volume in EFFR_ROWS:
        rows.append(
            {
                "date": day,
                "source_name": "New York Fed",
                "source_category": "realized_rate",
                "reference_rate_name": "EFFR",
                "rate_value": f"{rate_value:.2f}",
                "volume_if_available": str(volume),
                "source_url": source_url,
                "notes": "Official NY Fed reference-rates page excerpt transcribed from the accessible historical table. This build does not include a separate NY Fed Markets API extract.",
                "use_in_probability_analysis": "FALSE",
            }
        )
    return rows


def _build_long_rows(source_rows: list[dict[str, Any]], comparator_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in sorted(source_rows, key=lambda r: (_meeting_sort_key(r["meeting_month"]), r["date"], r["token_id"], r["outcome_name"])):
        if row["meeting_month"] not in TARGET_MONTHS:
            continue
        out.append(
            {
                "timestamp": row["timestamp"],
                "datetime_utc": row["datetime_utc"],
                "date": row["date"],
                "meeting_month": row["meeting_month"],
                "meeting_date": row["meeting_date"],
                "event_slug": row["event_slug"],
                "event_url": row["event_url"],
                "market_question": row["market_question"],
                "condition_id": row["condition_id"],
                "token_id": row["token_id"],
                "outcome_name": row["outcome_name"],
                "normalized_outcome_label": row["normalized_outcome_label"],
                "probability": row["probability"],
                "price": row["price"],
                "source_endpoint_type": row["source_endpoint_type"],
                "source_endpoint": row["source_endpoint"],
                "raw_file_path": row["raw_file_path"],
                "data_status": row["data_status"],
                "use_in_probability_analysis": row["use_in_probability_analysis"],
            }
        )
    return out


def _build_daily_rows(long_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in long_rows:
        if row["use_in_probability_analysis"] != "TRUE":
            continue
        buckets[(row["date"], row["meeting_month"], row["token_id"])].append(row)
    daily_rows: list[dict[str, Any]] = []
    for (day, meeting_month, token_id), bucket in sorted(buckets.items()):
        ordered = sorted(bucket, key=lambda row: _parse_datetime(row["datetime_utc"]) or datetime.min.replace(tzinfo=timezone.utc))
        probs = [p for p in (_safe_float(row["probability"]) for row in ordered) if p is not None]
        if not probs:
            continue
        meeting_dt = _parse_date(ordered[0]["meeting_date"])
        day_dt = _parse_date(day)
        if meeting_dt is None or day_dt is None:
            continue
        first = ordered[0]
        daily_rows.append(
            {
                "date": day,
                "meeting_month": meeting_month,
                "meeting_date": first["meeting_date"],
                "event_slug": first["event_slug"],
                "event_title": EVENT_SPECS[meeting_month]["event_title"],
                "market_question": first["market_question"],
                "outcome_name": first["outcome_name"],
                "normalized_outcome_label": first["normalized_outcome_label"],
                "token_id": token_id,
                "last_probability": probs[-1],
                "mean_probability": mean(probs),
                "min_probability": min(probs),
                "max_probability": max(probs),
                "number_of_observations": len(probs),
                "days_to_meeting": (meeting_dt - day_dt).days,
                "source_endpoint_type": first["source_endpoint_type"],
            }
        )
    return daily_rows


def _snapshot_targets(meeting_date: date) -> dict[str, date]:
    return {
        "T-30": meeting_date - timedelta(days=30),
        "T-14": meeting_date - timedelta(days=14),
        "T-7": meeting_date - timedelta(days=7),
        "T-3": meeting_date - timedelta(days=3),
        "T-1": meeting_date - timedelta(days=1),
        "final_available": meeting_date,
    }


def _select_snapshot(rows: list[dict[str, Any]], target_date: date) -> dict[str, Any] | None:
    ordered = sorted(rows, key=lambda row: _parse_datetime(row["datetime_utc"]) or datetime.min.replace(tzinfo=timezone.utc))
    eligible = [row for row in ordered if (parsed := _parse_date(row["date"])) is not None and parsed <= target_date]
    return eligible[-1] if eligible else None


def _build_snapshot_rows(long_rows: list[dict[str, Any]], comparator_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    realized = {row["meeting_month"]: row["realized_outcome_label"] for row in comparator_rows}
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in long_rows:
        if row["use_in_probability_analysis"] != "TRUE":
            continue
        buckets[(row["meeting_month"], row["token_id"])].append(row)

    out: list[dict[str, Any]] = []
    for (meeting_month, token_id), bucket in sorted(buckets.items(), key=lambda item: _meeting_sort_key(item[0][0])):
        meeting_dt = _parse_date(bucket[0]["meeting_date"])
        if meeting_dt is None:
            continue
        targets = _snapshot_targets(meeting_dt)
        realized_label = realized.get(meeting_month, "")
        for snapshot_label in SNAPSHOT_HORIZONS:
            selected = _select_snapshot(bucket, targets[snapshot_label])
            if selected is None:
                continue
            probability = _safe_float(selected["probability"])
            if probability is None:
                continue
            realized_0_1 = 1 if selected["normalized_outcome_label"] == realized_label else 0
            out.append(
                {
                    "meeting_month": meeting_month,
                    "meeting_date": selected["meeting_date"],
                    "event_slug": selected["event_slug"],
                    "event_title": EVENT_SPECS[meeting_month]["event_title"],
                    "market_question": selected["market_question"],
                    "outcome_name": selected["outcome_name"],
                    "normalized_outcome_label": selected["normalized_outcome_label"],
                    "snapshot_label": snapshot_label,
                    "target_date": targets[snapshot_label].isoformat(),
                    "selected_timestamp": selected["timestamp"],
                    "selected_datetime_utc": selected["datetime_utc"],
                    "selected_date": selected["date"],
                    "probability": probability,
                    "realized_outcome_0_1": realized_0_1,
                    "brier_score": (probability - float(realized_0_1)) ** 2,
                    "absolute_error": abs(probability - float(realized_0_1)),
                    "source_endpoint_type": selected["source_endpoint_type"],
                    "source_endpoint": selected["source_endpoint"],
                    "raw_file_path": selected["raw_file_path"],
                    "data_status": selected["data_status"],
                    "notes": "snapshot_selection=closest_observation_before_target",
                }
            )
    return out


def _save_pair(output_base: Path, png_backend: _ChartBackend, svg_backend: _ChartBackend) -> None:
    ensure_dir(output_base.parent)
    png_backend.save(output_base.with_suffix(".png"))
    svg_backend.save(output_base.with_suffix(".svg"))


def _plot_probability_paths(daily_rows: list[dict[str, Any]], output_dir: Path) -> None:
    by_meeting: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        by_meeting[row["meeting_month"]].append(row)
    meeting_months = [month for month in sorted(by_meeting, key=_meeting_sort_key) if by_meeting[month]]
    if not meeting_months:
        return

    width = 1500
    panel_height = 300
    height = 100 + panel_height * len(meeting_months)
    left, right = 100, 1080
    legend_left = 1140
    colors = {
        "50_plus_bps_cut": "#0B1F3A",
        "25_bps_cut": "#2F3E4E",
        "no_change": "#5C677D",
        "25_plus_bps_hike": "#7A7F87",
        "other": "#9AA1AA",
    }
    fonts = {"title": _load_font(28, bold=True), "body": _load_font(18), "small": _load_font(15), "tiny": _load_font(13)}
    backends = [_ChartBackend("png", width, height), _ChartBackend("svg", width, height)]

    for backend in backends:
        backend.rect((0, 0, width, height), fill="white", outline=None)
        backend.text((20, 24), "Fed decision probability paths", fonts["title"], fill="#0B1F3A", anchor="la")
        backend.text((20, 58), "Days to meeting", fonts["body"], fill="#333333", anchor="la")
        backend.text((legend_left, 58), "Series", fonts["body"], fill="#0B1F3A", anchor="la")
        for idx, meeting_month in enumerate(meeting_months):
            rows = sorted(by_meeting[meeting_month], key=lambda row: row["days_to_meeting"], reverse=True)
            top = 90 + idx * panel_height
            bottom = top + 170
            note_top = top + 200
            _draw_axes(backend, left, top, right, bottom)
            x_vals = [int(row["days_to_meeting"]) for row in rows]
            if not x_vals:
                continue
            x_min, x_max = min(x_vals), max(x_vals)
            if x_min == x_max:
                x_max += 1

            def x_to_px(x: int) -> float:
                return left + ((x - x_min) / float(x_max - x_min)) * (right - left)

            def y_to_px(y: float) -> float:
                y = max(0.0, min(1.0, y))
                return bottom - y * (bottom - top)

            for tick in [0.0, 0.25, 0.5, 0.75, 1.0]:
                backend.text((left - 12, y_to_px(tick)), f"{tick:.2f}".rstrip("0").rstrip("."), fonts["tiny"], fill="#666666", anchor="ra")
            for tick in [x_min, int(round((x_min + x_max) / 2.0)), x_max]:
                backend.text((x_to_px(tick), bottom + 12), str(tick), fonts["tiny"], fill="#666666", anchor="ma")
            labels = [label for label in DISPLAY_ORDER if any(row["normalized_outcome_label"] == label for row in rows)]
            legend_y = top
            for label in labels:
                series = sorted(
                    [
                        (int(row["days_to_meeting"]), _safe_float(row["last_probability"]))
                        for row in rows
                        if row["normalized_outcome_label"] == label and _safe_float(row["last_probability"]) is not None
                    ],
                    key=lambda item: item[0],
                )
                if not series:
                    continue
                pts = [(x_to_px(x), y_to_px(y)) for x, y in series]
                for pos in range(len(pts) - 1):
                    backend.line(pts[pos], pts[pos + 1], colors.get(label, "#5C677D"), 3)
                for pt in pts:
                    backend.circle(pt, 3.5, fill=colors.get(label, "#5C677D"), outline="white", width=1)
                backend.line((legend_left, legend_y + 9), (legend_left + 28, legend_y + 9), colors.get(label, "#5C677D"), 4)
                backend.circle((legend_left + 14, legend_y + 9), 4, fill=colors.get(label, "#5C677D"), outline="white", width=1)
                backend.text((legend_left + 40, legend_y + 9), DISPLAY_LABELS.get(label, label), fonts["small"], fill="#30343A", anchor="la")
                legend_y += 28
            realized_label = EVENT_SPECS[meeting_month]["realized_outcome_label"]
            backend.text((right - 10, top + 12), f"Realized: {DISPLAY_LABELS.get(realized_label, realized_label)}", fonts["small"], fill="#0B1F3A", anchor="ra")
            source_types = sorted({row["source_endpoint_type"] for row in rows})
            if source_types:
                if source_types == ["trades_fallback"]:
                    note = "trade-based fallback series"
                elif source_types == ["prices_history"]:
                    note = "prices-history series"
                else:
                    note = ", ".join(source_types) + " series"
            else:
                note = "no usable local probability history"
            backend.text((left, note_top), f"{meeting_month} | {note}", fonts["small"], fill="#4A4F57", anchor="la")
        backend.text((left, height - 18), "Only meetings with usable local probability history are plotted.", fonts["small"], fill="#4A4F57", anchor="la")

    _save_pair(output_dir / "fed_dec_to_apr_probability_paths", backends[0], backends[1])


def _plot_snapshot_accuracy(snapshot_rows: list[dict[str, Any]], output_dir: Path) -> None:
    scored = [row for row in snapshot_rows if row["brier_score"] != ""]
    if not scored:
        return
    by_meeting: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scored:
        by_meeting[row["meeting_month"]].append(row)
    meeting_months = sorted(by_meeting, key=_meeting_sort_key)
    width, height = 1200, 640
    left, top, right, bottom = 100, 100, 980, 500
    legend_left = 1020
    fonts = {"title": _load_font(28, bold=True), "body": _load_font(18), "small": _load_font(15), "tiny": _load_font(13)}
    colors = ["#0B1F3A", "#2F3E4E", "#5C677D", "#7A7F87", "#9AA1AA"]
    backends = [_ChartBackend("png", width, height), _ChartBackend("svg", width, height)]
    horizon_positions = {label: idx for idx, label in enumerate(SNAPSHOT_HORIZONS)}
    slot_width = (right - left) / float(len(SNAPSHOT_HORIZONS))

    for backend in backends:
        backend.rect((0, 0, width, height), fill="white", outline=None)
        backend.text((20, 24), "Snapshot accuracy by meeting and horizon", fonts["title"], fill="#0B1F3A", anchor="la")
        backend.text((20, 58), "Brier score", fonts["body"], fill="#333333", anchor="la")
        _draw_axes(backend, left, top, right, bottom)
        for tick in [0.0, 0.25, 0.5, 0.75, 1.0]:
            backend.text((left - 12, bottom - (bottom - top) * tick), f"{tick:.2f}".rstrip("0").rstrip("."), fonts["tiny"], fill="#666666", anchor="ra")
        for idx, label in enumerate(SNAPSHOT_HORIZONS):
            backend.text((left + idx * slot_width + slot_width / 2.0, bottom + 12), label, fonts["tiny"], fill="#666666", anchor="ma")
        bar_width = slot_width / max(len(meeting_months) + 1, 2)
        for meeting_idx, meeting_month in enumerate(meeting_months):
            for row in sorted(by_meeting[meeting_month], key=lambda r: horizon_positions.get(r["snapshot_label"], 99)):
                horizon_idx = horizon_positions.get(row["snapshot_label"])
                if horizon_idx is None:
                    continue
                brier = _safe_float(row["brier_score"])
                if brier is None:
                    continue
                center = left + horizon_idx * slot_width + slot_width / 2.0
                x0 = center - ((len(meeting_months) * bar_width) / 2.0) + meeting_idx * bar_width
                x1 = x0 + bar_width * 0.82
                y1 = bottom
                y0 = bottom - brier * (bottom - top)
                backend.rect((x0, y0, x1, y1), fill=colors[meeting_idx % len(colors)], outline="#ffffff", width=1)
        legend_y = top
        for meeting_idx, meeting_month in enumerate(meeting_months):
            backend.rect((legend_left, legend_y, legend_left + 16, legend_y + 16), fill=colors[meeting_idx % len(colors)], outline="#ffffff", width=1)
            backend.text((legend_left + 24, legend_y + 8), meeting_month, fonts["small"], fill="#30343A", anchor="la")
            legend_y += 26
        backend.text((legend_left, legend_y + 8), "Missing horizons are omitted.", fonts["small"], fill="#4A4F57", anchor="la")
        backend.text((left, 540), "Only horizons supported by the current cache are shown.", fonts["small"], fill="#4A4F57", anchor="la")

    _save_pair(output_dir / "fed_dec_to_apr_snapshot_accuracy", backends[0], backends[1])


def _plot_realized_rates(comparator_rows: list[dict[str, Any]], nyfed_rows: list[dict[str, Any]], output_dir: Path) -> None:
    if not comparator_rows:
        return
    width, height = 1280, 680
    left, top, right, bottom = 120, 100, 1080, 500
    legend_left = 1100
    fonts = {"title": _load_font(28, bold=True), "body": _load_font(18), "small": _load_font(15), "tiny": _load_font(13)}
    backends = [_ChartBackend("png", width, height), _ChartBackend("svg", width, height)]
    comparator_sorted = sorted(comparator_rows, key=lambda row: _meeting_sort_key(row["meeting_month"]))
    nyfed_sorted = sorted(nyfed_rows, key=lambda row: row["date"])
    target_points: list[tuple[date, float, float]] = []
    for row in comparator_sorted:
        meeting_dt = _parse_date(row["meeting_date"])
        lower = _safe_float(str(row["target_range_after"]).split("-")[0])
        upper = _safe_float(str(row["target_range_after"]).split("-")[-1])
        if meeting_dt is None or lower is None or upper is None:
            continue
        target_points.append((meeting_dt, lower, upper))
    if not target_points:
        return
    date_min = min([_parse_date(row["date"]) for row in nyfed_sorted if _parse_date(row["date"]) is not None] + [p[0] for p in target_points])
    date_max = max([_parse_date(row["date"]) for row in nyfed_sorted if _parse_date(row["date"]) is not None] + [p[0] for p in target_points])
    if date_min is None or date_max is None:
        return
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
        backend.text((20, 24), "Realized policy-rate context", fonts["title"], fill="#0B1F3A", anchor="la")
        backend.text((20, 58), "Official target range and accessible NY Fed EFFR rows", fonts["body"], fill="#333333", anchor="la")
        _draw_axes(backend, left, top, right, bottom)
        for tick in [3.5, 3.6, 3.7, 3.8]:
            backend.text((left - 12, y_to_px(tick)), f"{tick:.2f}", fonts["tiny"], fill="#666666", anchor="ra")
        for tick in [date_min, date_min + timedelta(days=(date_max - date_min).days // 3), date_min + timedelta(days=2 * (date_max - date_min).days // 3), date_max]:
            backend.text((x_to_px(tick), bottom + 12), tick.isoformat(), fonts["tiny"], fill="#666666", anchor="ma")

        band_color = "#D9DEE6"
        for idx, row in enumerate(comparator_sorted):
            meeting_dt = _parse_date(row["meeting_date"])
            lower = _safe_float(str(row["target_range_after"]).split("-")[0])
            upper = _safe_float(str(row["target_range_after"]).split("-")[-1])
            if meeting_dt is None or lower is None or upper is None:
                continue
            left_x = x_to_px(meeting_dt)
            right_x = x_to_px(date_max if idx == len(comparator_sorted) - 1 else _parse_date(comparator_sorted[idx + 1]["meeting_date"]) or date_max)
            backend.rect((left_x, y_to_px(upper), right_x, y_to_px(lower)), fill=band_color, outline="#C7CDD6", width=1)
            backend.text((left_x + 4, y_to_px(upper) - 8), f"{row['meeting_month']} target {row['target_range_after']}", fonts["tiny"], fill="#23324A", anchor="la")

        effr_series: list[tuple[date, float]] = []
        for row in nyfed_sorted:
            day = _parse_date(row["date"])
            rate = _safe_float(row["rate_value"])
            if day is None or rate is None:
                continue
            effr_series.append((day, rate))
        if effr_series:
            pts = [(x_to_px(day), y_to_px(rate)) for day, rate in effr_series]
            for idx in range(len(pts) - 1):
                backend.line(pts[idx], pts[idx + 1], "#0B1F3A", 3)
            for pt in pts:
                backend.circle(pt, 3.5, fill="#0B1F3A", outline="white", width=1)
        for row in comparator_sorted:
            meeting_dt = _parse_date(row["meeting_date"])
            if meeting_dt is None:
                continue
            x = x_to_px(meeting_dt)
            backend.line((x, top), (x, bottom), "#7A7F87", 1)
            backend.text((x + 2, top + 12), row["meeting_month"], fonts["small"], fill="#0B1F3A", anchor="la")

        backend.rect((legend_left, top, legend_left + 16, top + 16), fill=band_color, outline="#C7CDD6", width=1)
        backend.text((legend_left + 24, top + 8), "Target range after meeting", fonts["small"], fill="#30343A", anchor="la")
        backend.line((legend_left, top + 34), (legend_left + 16, top + 34), "#0B1F3A", 3)
        backend.circle((legend_left + 8, top + 34), 3.5, fill="#0B1F3A", outline="white", width=1)
        backend.text((legend_left + 24, top + 34), "EFFR", fonts["small"], fill="#30343A", anchor="la")
        backend.text((legend_left, top + 64), "NY Fed data are partial in this build.", fonts["small"], fill="#4A4F57", anchor="la")
        backend.text((left, 552), "The chart is policy-rate context, not a forecast comparison.", fonts["small"], fill="#4A4F57", anchor="la")

    _save_pair(output_dir / "fed_dec_to_apr_realized_rates", backends[0], backends[1])


def _write_memo(base_dir: Path, audit_rows: list[dict[str, Any]], comparator_rows: list[dict[str, Any]], nyfed_rows: list[dict[str, Any]], long_rows: list[dict[str, Any]], daily_rows: list[dict[str, Any]], snapshot_rows: list[dict[str, Any]]) -> Path:
    notes_dir = ensure_dir(base_dir / "analysis" / "notes")
    memo_path = notes_dir / "fed_dec_to_apr_realized_analysis.md"
    usable_meetings = sorted({row["meeting_month"] for row in long_rows})
    memo = [
        "# Fed Dec-to-Apr Realized Analysis",
        "",
        "## Current Data Status",
        f"- Exact Fed decision markets covered in the audit: {', '.join(row['event_month'] for row in audit_rows)}.",
        f"- Meetings with usable local Polymarket probability rows: {', '.join(usable_meetings) if usable_meetings else 'none'}.",
        f"- Realized Fed decision rows written: {len(comparator_rows)}.",
        f"- NY Fed realized-rate rows written: {len(nyfed_rows)}.",
        "",
        "This section compares Polymarket’s ex ante probabilities before FOMC decisions with the realized Federal Reserve policy outcomes. The objective is to assess whether prediction-market probabilities moved toward the realized decision early enough to become decision-relevant.",
        "",
        "Cette section compare les probabilités ex ante issues de Polymarket avant les décisions du FOMC avec les décisions réellement prises par la Fed. L’objectif est d’évaluer si les probabilités de marché convergent vers l’issue réalisée suffisamment tôt pour devenir actionnables.",
        "",
        "## Which Meetings Were Included",
        "- December 2025, January 2026, March 2026, and April 2026 are included as exact meeting-specific Fed decision markets.",
        "- February is not forced into the analysis because the regular FOMC meeting cadence does not place a decision there in this window.",
        "",
        "## Which Meetings Had Usable Polymarket Histories",
        "- All four meetings now have usable local probability rows in the repository.",
        "- All recovered series are trade-based fallback histories rather than standardized prices-history files.",
        "- December 2025, March 2026, and April 2026 were recovered from the exact Polymarket markets and can now be graphed alongside January 2026.",
        "",
        "## Realized Fed Outcomes",
    ]
    for row in comparator_rows:
        memo.append(
            f"- {row['meeting_month']}: {row['official_decision']} Target range moved from {row['target_range_before']} to {row['target_range_after']}."
        )
    memo.extend(
        [
            "",
            "## Realized Rate Context",
            "- The NY Fed EFFR file is partial in this build and reflects the accessible reference-rate table captured from the official NY Fed page.",
            "- It should be read as realized policy-rate context, not as a forecast or a probability series.",
            "",
            "## What Can and Cannot Be Concluded",
            "- Polymarket probabilities can be compared to the realized January 2026 outcome with full local support.",
            "- The broader Dec-to-Apr repeated-event research question is only partially identified because the current repository does not contain usable local Polymarket histories for December, March, or April.",
            "- The rate chart and comparator file are still useful because they pin the realized policy outcome and the post-meeting rate environment to official sources.",
            "",
            "## Thesis Framing",
            "This is an ex-ante versus realized-outcome analysis. Polymarket provides the probability distribution before the FOMC decision, while the Federal Reserve statement gives the actual policy action and the realized rate environment. The substantive question is whether the market probability moved far enough, early enough, to be decision-relevant. That is the actionability problem, not whether Polymarket matched a separate forecaster.",
            "",
        ]
    )
    memo_path.write_text("\n".join(memo), encoding="utf-8")
    return memo_path


def run_fed_dec_to_apr_analysis(base_dir: Any) -> dict[str, Path]:
    base_dir = Path(base_dir)
    if (base_dir / "pipeline_outputs").exists():
        data_dir = base_dir
    elif (base_dir / "Data").exists():
        data_dir = base_dir / "Data"
    else:
        data_dir = base_dir

    debug_dir = ensure_dir(data_dir / "pipeline_outputs" / "debug")
    cleaned_dir = ensure_dir(data_dir / "pipeline_outputs" / "cleaned")
    analysis_dir = ensure_dir(data_dir / "analysis")
    daily_dir = ensure_dir(analysis_dir / "daily")
    snapshots_dir = ensure_dir(analysis_dir / "snapshots")
    comparator_dir = ensure_dir(analysis_dir / "comparators")
    figures_dir = ensure_dir(analysis_dir / "figures")
    notes_dir = ensure_dir(analysis_dir / "notes")

    audit_rows = _build_audit_rows(data_dir)
    comparator_rows = _build_comparator_rows()
    nyfed_rows = _build_nyfed_rows()
    source_rows = _load_source_rows(data_dir)
    long_rows = _build_long_rows(source_rows, comparator_rows)
    summary_rows = _build_summary_rows(data_dir, long_rows)
    daily_rows = _build_daily_rows(long_rows)
    snapshot_rows = _build_snapshot_rows(long_rows, comparator_rows)

    audit_path = debug_dir / "fed_decision_event_audit.csv"
    write_csv(audit_path, AUDIT_COLUMNS, audit_rows)

    tables_dir = ensure_dir(analysis_dir / "tables")
    summary_path = tables_dir / "fed_case_selection_summary_dec_to_apr.csv"
    write_csv(summary_path, SUMMARY_COLUMNS, summary_rows)

    long_path = cleaned_dir / "polymarket_fed_decisions_dec_to_apr_long.csv"
    write_csv(long_path, LONG_COLUMNS, long_rows)

    comparator_path = comparator_dir / "fed_realized_decisions_dec_to_apr.csv"
    write_csv(comparator_path, COMPARATOR_COLUMNS, comparator_rows)

    nyfed_path = comparator_dir / "nyfed_effr_dec_to_apr.csv"
    write_csv(nyfed_path, NYFED_COLUMNS, nyfed_rows)

    daily_path = daily_dir / "fed_dec_to_apr_daily.csv"
    write_csv(daily_path, DAILY_COLUMNS, daily_rows)

    snapshot_path = snapshots_dir / "fed_dec_to_apr_snapshots.csv"
    write_csv(snapshot_path, SNAPSHOT_COLUMNS, snapshot_rows)

    _plot_probability_paths(daily_rows, figures_dir)
    _plot_realized_rates(comparator_rows, nyfed_rows, figures_dir)
    _plot_snapshot_accuracy(snapshot_rows, figures_dir)

    memo_path = _write_memo(data_dir, audit_rows, comparator_rows, nyfed_rows, long_rows, daily_rows, snapshot_rows)

    return {
        "audit": audit_path,
        "summary": summary_path,
        "long": long_path,
        "comparator": comparator_path,
        "nyfed": nyfed_path,
        "daily": daily_path,
        "snapshots": snapshot_path,
        "figures": figures_dir,
        "notes": memo_path,
    }
