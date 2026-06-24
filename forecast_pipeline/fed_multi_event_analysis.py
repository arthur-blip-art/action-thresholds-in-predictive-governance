"""Repeated-event Fed analysis built from local Polymarket artifacts only.

This module is intentionally conservative:
- it only uses exact month-specific FOMC decision markets
- it does not fetch any new data
- it labels trade-based fallback rows explicitly
- it keeps realized comparisons limited to meetings with official Fed statements
- it does not fabricate missing snapshot horizons
"""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

from .io_utils import ensure_dir, read_csv_rows, write_csv
from .thesis_analysis import _ChartBackend, _draw_axes, _load_font, _wrap_text


RAW_BASE = "https://data-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

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

COMBINED_LONG_COLUMNS = (
    "timestamp",
    "datetime_utc",
    "date",
    "meeting_month",
    "meeting_date",
    "event_slug",
    "event_title",
    "market_slug",
    "market_question",
    "condition_id",
    "token_id",
    "outcome_name",
    "normalized_outcome_label",
    "price",
    "probability",
    "source_endpoint",
    "source_endpoint_type",
    "raw_file_path",
    "data_status",
    "use_in_probability_analysis",
)

COMPARATOR_COLUMNS = (
    "meeting_date",
    "meeting_month",
    "official_decision",
    "realized_outcome_label",
    "target_range_before",
    "target_range_after",
    "decision_size_bps",
    "source_name",
    "source_url",
    "notes",
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
    "first_probability",
    "last_probability",
    "mean_probability",
    "min_probability",
    "max_probability",
    "number_of_observations",
    "source_endpoint_type",
    "days_to_meeting",
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


@dataclass(frozen=True)
class FedDecisionSpec:
    event_month: str
    event_slug: str
    event_url: str
    event_title: str
    event_question: str
    accepted_true_false: str
    rejection_reason: str
    notes: str = ""
    official_decision: str = ""
    realized_outcome_label: str = ""
    target_range_before: str = ""
    target_range_after: str = ""
    decision_size_bps: str = ""
    source_url: str = ""
    source_name: str = "Federal Reserve"


FED_CANDIDATES: tuple[FedDecisionSpec, ...] = (
    FedDecisionSpec(
        event_month="2025-12",
        event_slug="fed-decision-in-december",
        event_url="https://polymarket.com/event/fed-decision-in-december",
        event_title="Fed decision in December?",
        event_question="Fed decision in December?",
        accepted_true_false="TRUE",
        rejection_reason="",
        notes="Exact December 2025 FOMC decision market; local prices_history cache exists but is currently unusable (invalid-filter error payloads only).",
        official_decision="Lowered the target range by 25 basis points.",
        realized_outcome_label="25_bps_cut",
        target_range_before="3.75-4.00",
        target_range_after="3.50-3.75",
        decision_size_bps="25",
        source_url="https://www.federalreserve.gov/newsevents/pressreleases/monetary20251210a1.htm",
    ),
    FedDecisionSpec(
        event_month="2026-01",
        event_slug="fed-decision-in-january",
        event_url="https://polymarket.com/event/fed-decision-in-january",
        event_title="Fed decision in January?",
        event_question="Fed decision in January?",
        accepted_true_false="TRUE",
        rejection_reason="",
        notes="Exact January 2026 FOMC decision market; local trade-based fallback series is available.",
        official_decision="Maintained the target range.",
        realized_outcome_label="no_change",
        target_range_before="3.50-3.75",
        target_range_after="3.50-3.75",
        decision_size_bps="0",
        source_url="https://www.federalreserve.gov/newsevents/pressreleases/monetary20260128a1.htm",
    ),
    FedDecisionSpec(
        event_month="2026-06",
        event_slug="fed-decision-in-june-825",
        event_url="https://polymarket.com/event/fed-decision-in-june-825",
        event_title="Fed decision in June?",
        event_question="Fed decision in June?",
        accepted_true_false="TRUE",
        rejection_reason="",
        notes="Exact June 2026 FOMC decision market; local prices_history cache is usable, but the meeting is still upcoming as of the current date.",
    ),
    FedDecisionSpec(
        event_month="2026-07",
        event_slug="fed-decision-in-july-181",
        event_url="https://polymarket.com/event/fed-decision-in-july-181",
        event_title="Fed decision in July?",
        event_question="Fed decision in July?",
        accepted_true_false="TRUE",
        rejection_reason="",
        notes="Exact July 2026 FOMC decision market; local prices_history cache only contains invalid-filter error payloads, and no usable trade fallback rows were recovered.",
    ),
    FedDecisionSpec(
        event_month="2026-09",
        event_slug="fed-decision-in-september-762",
        event_url="https://polymarket.com/event/fed-decision-in-september-762",
        event_title="Fed decision in September?",
        event_question="Fed decision in September?",
        accepted_true_false="TRUE",
        rejection_reason="",
        notes="Exact September 2026 FOMC decision market; local prices_history cache only contains invalid-filter error payloads, and no usable trade fallback rows were recovered.",
    ),
    FedDecisionSpec(
        event_month="2026-03",
        event_slug="fed-decision-in-march",
        event_url="https://polymarket.com/event/fed-decision-in-march",
        event_title="Fed decision in March?",
        event_question="Fed decision in March?",
        accepted_true_false="FALSE",
        rejection_reason="No exact local Polymarket artifact found for March.",
        notes="Requested candidate not present in local Polymarket artifacts.",
    ),
    FedDecisionSpec(
        event_month="2026-04",
        event_slug="fed-decision-in-april",
        event_url="https://polymarket.com/event/fed-decision-in-april",
        event_title="Fed decision in April?",
        event_question="Fed decision in April?",
        accepted_true_false="FALSE",
        rejection_reason="No exact local Polymarket artifact found for April.",
        notes="Requested candidate not present in local Polymarket artifacts.",
    ),
)


TARGET_LABELS = {
    "50_plus_bps_cut": "50+ bps cut",
    "25_bps_cut": "25 bps cut",
    "no_change": "no change",
    "25_plus_bps_hike": "25+ bps hike",
    "other": "other",
}

DISPLAY_ORDER = ["50_plus_bps_cut", "25_bps_cut", "no_change", "25_plus_bps_hike", "other"]
SNAPSHOT_HORIZONS = ["T-30", "T-14", "T-7", "T-3", "T-1", "final_available"]

REALIZED_DECISIONS = {
    "2025-12": {
        "meeting_date": "2025-12-10",
        "official_decision": "Lowered the target range by 25 basis points.",
        "realized_outcome_label": "25_bps_cut",
        "target_range_before": "3.75-4.00",
        "target_range_after": "3.50-3.75",
        "decision_size_bps": "25",
        "source_name": "Federal Reserve",
        "source_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20251210a1.htm",
        "notes": "Official FOMC statement after the December 2025 meeting.",
    },
    "2026-01": {
        "meeting_date": "2026-01-28",
        "official_decision": "Maintained the target range.",
        "realized_outcome_label": "no_change",
        "target_range_before": "3.50-3.75",
        "target_range_after": "3.50-3.75",
        "decision_size_bps": "0",
        "source_name": "Federal Reserve",
        "source_url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260128a1.htm",
        "notes": "Official FOMC statement after the January 2026 meeting.",
    },
}


def _parse_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        return date.fromisoformat(value[:10])
    except Exception:
        return None


def _safe_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _format_datetime_utc(value: Optional[datetime]) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_date(value: Optional[date]) -> str:
    return value.isoformat() if value else ""


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _json_array(value: Any) -> list[str]:
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


def _extract_history_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("history", "prices_history", "candles", "candlesticks", "results", "items", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _source_endpoint_type(source_endpoint: str) -> str:
    if "trades" in source_endpoint:
        return "trades_fallback"
    return "prices_history"


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
        if "increase" in question:
            return "25_plus_bps_hike"
    if "increase" in question or "hike" in question:
        return "25_plus_bps_hike"
    return "other"


def _selected_label_from_question(market_question: str, outcome_name: str) -> str:
    normalized = _normalize_outcome_label(market_question, outcome_name)
    return normalized if normalized in TARGET_LABELS else "other"


def _load_event_payload(raw_dir: Path, event_slug: str) -> Tuple[Optional[dict[str, Any]], Optional[Path]]:
    candidates = sorted(raw_dir.glob(f"*event_{event_slug}.json"))
    if not candidates:
        return None, None
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        payload = _load_json(latest)
    except Exception:
        return None, latest
    if isinstance(payload, list):
        payload = payload[0] if payload and isinstance(payload[0], dict) else None
    if not isinstance(payload, dict):
        return None, latest
    return payload, latest


def _summarize_event_payload(payload: Optional[dict[str, Any]]) -> Tuple[str, str, str, str]:
    if not isinstance(payload, dict):
        return "", "", "", ""
    markets = payload.get("markets") if isinstance(payload.get("markets"), list) else []
    market_count = len([m for m in markets if isinstance(m, dict)])
    token_ids: list[str] = []
    total_volume = 0.0
    total_liquidity = 0.0
    for market in markets:
        if not isinstance(market, dict):
            continue
        token_ids.extend(_json_array(market.get("clobTokenIds") or market.get("clob_token_ids") or market.get("tokenIds") or market.get("token_ids")))
        total_volume += _safe_float(market.get("volume")) or _safe_float(market.get("volumeNum")) or 0.0
        total_liquidity += (
            _safe_float(market.get("liquidity"))
            or _safe_float(market.get("liquidityNum"))
            or _safe_float(market.get("totalLiquidity"))
            or 0.0
        )
    return str(market_count), str(len(set(token_ids))), f"{total_volume:.6f}".rstrip("0").rstrip("."), f"{total_liquidity:.6f}".rstrip("0").rstrip(".")


def _iter_market_tokens(payload: dict[str, Any], event_slug: str, event_title: str) -> list[dict[str, Any]]:
    markets = payload.get("markets") if isinstance(payload.get("markets"), list) else []
    tokens: list[dict[str, Any]] = []
    for market in markets:
        if not isinstance(market, dict):
            continue
        market_slug = _first_nonempty(market.get("slug"), market.get("id"), market.get("question"))
        market_question = _first_nonempty(market.get("question"), market.get("title"), market.get("name"), market_slug)
        condition_id = _first_nonempty(market.get("conditionId"), market.get("condition_id"), market.get("conditionID"))
        outcome_names = _json_array(market.get("outcomes") or market.get("tokens"))
        token_ids = _json_array(market.get("clobTokenIds") or market.get("clob_token_ids") or market.get("tokenIds") or market.get("token_ids"))
        volume = _first_nonempty(market.get("volume"), market.get("volumeNum"), market.get("volumeClob"))
        liquidity = _first_nonempty(market.get("liquidity"), market.get("liquidityNum"), market.get("liquidityClob"), market.get("totalLiquidity"))
        for idx, token_id in enumerate(token_ids):
            outcome_name = outcome_names[idx] if idx < len(outcome_names) else (outcome_names[0] if outcome_names else "")
            tokens.append(
                {
                    "event_slug": event_slug,
                    "event_title": event_title,
                    "market_slug": market_slug,
                    "market_question": market_question,
                    "condition_id": condition_id,
                    "token_id": token_id,
                    "outcome_name": outcome_name,
                    "raw_outcome_name": outcome_name,
                    "volume": volume,
                    "liquidity": liquidity,
                    "market_payload": market,
                }
            )
    return tokens


def _dedupe_keep_latest(files: list[Path], key_fn) -> list[Path]:
    selected: dict[Any, Path] = {}
    for path in files:
        key = key_fn(path)
        if key is None:
            continue
        current = selected.get(key)
        if current is None or path.stat().st_mtime > current.stat().st_mtime:
            selected[key] = path
    return [selected[key] for key in sorted(selected)]


def _extract_offset(path: Path) -> Optional[int]:
    match = re.search(r"_trades_[^_]+_(\d+)\.txt$", path.name)
    if not match:
        return 0 if "_trades_" in path.name else None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _convert_prices_history_rows(
    rows: list[dict[str, Any]],
    token: dict[str, Any],
    raw_path: Path,
    source_endpoint: str,
    meeting_month: str,
    meeting_date: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in rows:
        timestamp = item.get("t") or item.get("timestamp") or item.get("time") or item.get("ts")
        price = item.get("p") or item.get("price") or item.get("value")
        ts = _safe_float(timestamp)
        pr = _safe_float(price)
        if ts is None or pr is None:
            continue
        if not (0.0 <= pr <= 1.0):
            continue
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        out.append(
            {
                "timestamp": int(ts),
                "datetime_utc": _format_datetime_utc(dt),
                "date": dt.date().isoformat(),
                "meeting_month": meeting_month,
                "meeting_date": meeting_date,
                "event_slug": token["event_slug"],
                "event_title": token["event_title"],
                "market_slug": token["market_slug"],
                "market_question": token["market_question"],
                "condition_id": token["condition_id"],
                "token_id": token["token_id"],
                "outcome_name": token["outcome_name"],
                "normalized_outcome_label": _selected_label_from_question(token["market_question"], token["outcome_name"]),
                "price": pr,
                "probability": pr,
                "source_endpoint": source_endpoint,
                "source_endpoint_type": "prices_history",
                "raw_file_path": str(raw_path),
                "data_status": "local_cached_raw",
                "use_in_probability_analysis": "TRUE" if token["outcome_name"].lower() == "yes" else "FALSE",
                "_sort_dt": dt,
            }
        )
    return out


def _convert_trade_rows(
    rows: list[dict[str, Any]],
    token: dict[str, Any],
    raw_path: Path,
    source_endpoint: str,
    meeting_month: str,
    meeting_date: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in rows:
        if _first_nonempty(item.get("asset")) != token["token_id"]:
            continue
        timestamp = _safe_float(item.get("timestamp"))
        price = _safe_float(item.get("price"))
        if timestamp is None or price is None:
            continue
        if not (0.0 <= price <= 1.0):
            continue
        tx_hash = _first_nonempty(item.get("transactionHash"))
        dedupe_key = (tx_hash, int(timestamp), price, _first_nonempty(item.get("side")), _first_nonempty(item.get("outcome")))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        dt = datetime.fromtimestamp(int(timestamp), tz=timezone.utc)
        out.append(
            {
                "timestamp": int(timestamp),
                "datetime_utc": _format_datetime_utc(dt),
                "date": dt.date().isoformat(),
                "meeting_month": meeting_month,
                "meeting_date": meeting_date,
                "event_slug": token["event_slug"],
                "event_title": token["event_title"],
                "market_slug": token["market_slug"],
                "market_question": token["market_question"],
                "condition_id": token["condition_id"],
                "token_id": token["token_id"],
                "outcome_name": token["outcome_name"],
                "normalized_outcome_label": _selected_label_from_question(token["market_question"], token["outcome_name"]),
                "price": price,
                "probability": price,
                "source_endpoint": source_endpoint,
                "source_endpoint_type": "trades_fallback",
                "raw_file_path": str(raw_path),
                "data_status": "local_cached_raw",
                "use_in_probability_analysis": "TRUE" if token["outcome_name"].lower() == "yes" else "FALSE",
                "_sort_dt": dt,
            }
        )
    return out


def _load_token_rows(raw_dir: Path, token: dict[str, Any], meeting_month: str, meeting_date: str) -> tuple[list[dict[str, Any]], str]:
    token_id = token["token_id"]
    condition_id = token["condition_id"]

    price_files = sorted(raw_dir.glob(f"*prices_history_{token_id}*.txt"))
    valid_price_candidates: list[tuple[int, float, Path, list[dict[str, Any]]]] = []
    for path in price_files:
        try:
            payload = _load_json(path)
        except Exception:
            continue
        history_rows = _extract_history_rows(payload)
        if history_rows:
            valid_price_candidates.append((len(history_rows), path.stat().st_mtime, path, history_rows))
    if valid_price_candidates:
        _, _, raw_path, history_rows = max(valid_price_candidates, key=lambda item: (item[0], item[1]))
        source_endpoint = f"{CLOB_BASE}/prices-history?market={token_id}"
        return _convert_prices_history_rows(history_rows, token, raw_path, source_endpoint, meeting_month, meeting_date), "prices_history"

    trade_files = sorted(raw_dir.glob(f"*trades_{token_id}_*.txt"))
    trade_pages = _dedupe_keep_latest(trade_files, _extract_offset)
    trade_rows: list[dict[str, Any]] = []
    for path in trade_pages:
        try:
            payload = _load_json(path)
        except Exception:
            continue
        if not isinstance(payload, list):
            continue
        offset = _extract_offset(path) or 0
        source_endpoint = f"{RAW_BASE}/trades?market={condition_id}&limit=1000&offset={offset}"
        trade_rows.extend(_convert_trade_rows(payload, token, path, source_endpoint, meeting_month, meeting_date))
    return trade_rows, "trades_fallback" if trade_rows else ""


def _load_event_rows(raw_dir: Path, spec: FedDecisionSpec) -> Tuple[List[dict[str, Any]], Optional[dict[str, Any]], List[str]]:
    payload, _ = _load_event_payload(raw_dir, spec.event_slug)
    if not isinstance(payload, dict):
        return [], None, []
    event_title = _first_nonempty(payload.get("title"), spec.event_title)
    meeting_date = _format_date(_parse_date(_first_nonempty(payload.get("endDateIso"), payload.get("endDate"), spec.event_month + "-01")))
    tokens = _iter_market_tokens(payload, _first_nonempty(payload.get("slug"), spec.event_slug), event_title)
    combined_rows: list[dict[str, Any]] = []
    source_types: list[str] = []
    for token in tokens:
        rows, source_type = _load_token_rows(raw_dir, token, spec.event_month, meeting_date)
        if rows:
            combined_rows.extend(rows)
            source_types.append(source_type)
    return combined_rows, payload, source_types


def _load_local_event_rows(base_dir: Path, spec: FedDecisionSpec) -> Tuple[List[dict[str, Any]], Optional[dict[str, Any]], List[str], Optional[Path]]:
    raw_dir = base_dir / "pipeline_outputs" / "raw" / "polymarket"
    rows, payload, source_types = _load_event_rows(raw_dir, spec)
    payload_path = None
    if payload is not None:
        event_path = sorted(raw_dir.glob(f"*event_{spec.event_slug}.json"))
        if event_path:
            payload_path = event_path[-1]
    return rows, payload, source_types, payload_path


def _build_audit_rows(base_dir: Path) -> tuple[list[dict[str, Any]], set[str]]:
    rows: list[dict[str, Any]] = []
    accepted_event_slugs: set[str] = set()
    raw_dir = base_dir / "pipeline_outputs" / "raw" / "polymarket"
    for spec in FED_CANDIDATES:
        payload, _ = _load_event_payload(raw_dir, spec.event_slug)
        markets_found, token_ids_found, volume, liquidity = _summarize_event_payload(payload)
        usable_rows, _, source_types, _ = _load_local_event_rows(base_dir, spec)
        has_rows = bool(usable_rows)
        if spec.accepted_true_false == "TRUE":
            accepted_event_slugs.add(spec.event_slug)
        notes = spec.notes
        if spec.accepted_true_false == "TRUE" and not has_rows:
            notes = f"{notes} No usable history rows were recovered from the local cache."
        elif has_rows:
            notes = f"{notes} Local rows recovered: {len(usable_rows)}."
        rows.append(
            {
                "event_month": spec.event_month,
                "event_slug": spec.event_slug,
                "event_url": spec.event_url,
                "event_title": spec.event_title if payload or spec.accepted_true_false == "TRUE" else "",
                "event_question": spec.event_question if payload or spec.accepted_true_false == "TRUE" else "",
                "accepted_true_false": spec.accepted_true_false,
                "rejection_reason": spec.rejection_reason if spec.accepted_true_false == "FALSE" else "",
                "markets_found": markets_found if payload else ("0" if spec.accepted_true_false == "FALSE" else ""),
                "token_ids_found": token_ids_found if payload else ("0" if spec.accepted_true_false == "FALSE" else ""),
                "volume": volume,
                "liquidity": liquidity,
                "notes": notes.strip(),
            }
        )
    return rows, accepted_event_slugs


def _build_comparator_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for meeting_month, info in REALIZED_DECISIONS.items():
        rows.append(
            {
                "meeting_date": info["meeting_date"],
                "meeting_month": meeting_month,
                "official_decision": info["official_decision"],
                "realized_outcome_label": info["realized_outcome_label"],
                "target_range_before": info["target_range_before"],
                "target_range_after": info["target_range_after"],
                "decision_size_bps": info["decision_size_bps"],
                "source_name": info["source_name"],
                "source_url": info["source_url"],
                "notes": info["notes"],
            }
        )
    return rows


def _build_combined_rows(all_rows: list[dict[str, Any]], accepted_event_slugs: set[str]) -> list[dict[str, Any]]:
    filtered = [row for row in all_rows if row["event_slug"] in accepted_event_slugs]
    filtered = [row for row in filtered if row["normalized_outcome_label"] in TARGET_LABELS]
    out: list[dict[str, Any]] = []
    for row in sorted(filtered, key=lambda item: (item["meeting_month"], item["_sort_dt"], item["token_id"], item["outcome_name"])):
        out.append(
            {
                "timestamp": row["timestamp"],
                "datetime_utc": row["datetime_utc"],
                "date": row["date"],
                "meeting_month": row["meeting_month"],
                "meeting_date": row["meeting_date"],
                "event_slug": row["event_slug"],
                "event_title": row["event_title"],
                "market_slug": row["market_slug"],
                "market_question": row["market_question"],
                "condition_id": row["condition_id"],
                "token_id": row["token_id"],
                "outcome_name": row["outcome_name"],
                "normalized_outcome_label": row["normalized_outcome_label"],
                "price": f"{row['price']:.12g}",
                "probability": f"{row['probability']:.12g}",
                "source_endpoint": row["source_endpoint"],
                "source_endpoint_type": row["source_endpoint_type"],
                "raw_file_path": row["raw_file_path"],
                "data_status": row["data_status"],
                "use_in_probability_analysis": row["use_in_probability_analysis"],
            }
        )
    return out


def _build_daily_rows(combined_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in combined_rows:
        if row["use_in_probability_analysis"] != "TRUE":
            continue
        buckets[
            (
                row["date"],
                row["meeting_month"],
                row["meeting_date"],
                row["event_slug"],
                row["token_id"],
            )
        ].append(row)

    daily_rows: list[dict[str, Any]] = []
    for (day, meeting_month, meeting_date, event_slug, token_id), bucket in sorted(buckets.items()):
        ordered = sorted(bucket, key=lambda row: _parse_datetime(row["datetime_utc"]) or datetime.min.replace(tzinfo=timezone.utc))
        probs = [_safe_float(row["probability"]) for row in ordered]
        probs = [prob for prob in probs if prob is not None]
        if not probs:
            continue
        first = ordered[0]
        meeting_dt = _parse_date(meeting_date)
        day_dt = _parse_date(day)
        if meeting_dt is None or day_dt is None:
            continue
        daily_rows.append(
            {
                "date": day,
                "meeting_month": meeting_month,
                "meeting_date": meeting_date,
                "event_slug": event_slug,
                "event_title": first["event_title"],
                "market_question": first["market_question"],
                "outcome_name": first["outcome_name"],
                "normalized_outcome_label": first["normalized_outcome_label"],
                "token_id": token_id,
                "first_probability": probs[0],
                "last_probability": probs[-1],
                "mean_probability": mean(probs),
                "min_probability": min(probs),
                "max_probability": max(probs),
                "number_of_observations": len(probs),
                "source_endpoint_type": first["source_endpoint_type"],
                "days_to_meeting": (meeting_dt - day_dt).days,
            }
        )
    return daily_rows


def _snapshot_horizon_target(meeting_date: date) -> Dict[str, Optional[date]]:
    return {
        "T-30": meeting_date - timedelta(days=30),
        "T-14": meeting_date - timedelta(days=14),
        "T-7": meeting_date - timedelta(days=7),
        "T-3": meeting_date - timedelta(days=3),
        "T-1": meeting_date - timedelta(days=1),
        "final_available": meeting_date,
    }


def _select_snapshot_rows(rows: List[dict[str, Any]], target_date: Optional[date]) -> Tuple[Optional[dict[str, Any]], str]:
    if not rows:
        return None, "no_data"
    ordered = sorted(rows, key=lambda row: _parse_datetime(row["datetime_utc"]) or datetime.min.replace(tzinfo=timezone.utc))
    if target_date is None:
        return ordered[-1], "no_target_date"
    eligible = [row for row in ordered if _parse_date(row["date"]) is not None and _parse_date(row["date"]) <= target_date]
    if eligible:
        return eligible[-1], "before_or_on_target"
    return None, "no_observation_before_target"


def _build_snapshot_rows(combined_rows: list[dict[str, Any]], comparator_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    realized_map = {row["meeting_month"]: row for row in comparator_rows}
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in combined_rows:
        if row["use_in_probability_analysis"] != "TRUE":
            continue
        buckets[(row["meeting_month"], row["token_id"])].append(row)

    snapshot_rows: list[dict[str, Any]] = []
    for (meeting_month, token_id), bucket in sorted(buckets.items()):
        meeting_date = _parse_date(bucket[0]["meeting_date"])
        if meeting_date is None:
            continue
        targets = _snapshot_horizon_target(meeting_date)
        realized_label = realized_map.get(meeting_month, {}).get("realized_outcome_label", "")
        ordered = sorted(bucket, key=lambda row: _parse_datetime(row["datetime_utc"]) or datetime.min.replace(tzinfo=timezone.utc))
        for snapshot_label in SNAPSHOT_HORIZONS:
            target_date = targets[snapshot_label]
            selected, selection_mode = _select_snapshot_rows(ordered, target_date)
            if selected is None:
                continue
            probability = _safe_float(selected["probability"])
            if probability is None:
                continue
            realized_0_1: Any = ""
            brier_score: Any = ""
            absolute_error: Any = ""
            if realized_label:
                realized_0_1 = 1 if selected["normalized_outcome_label"] == realized_label else 0
                brier_score = (probability - float(realized_0_1)) ** 2
                absolute_error = abs(probability - float(realized_0_1))
            snapshot_rows.append(
                {
                    "meeting_month": meeting_month,
                    "meeting_date": bucket[0]["meeting_date"],
                    "event_slug": selected["event_slug"],
                    "event_title": selected["event_title"],
                    "market_question": selected["market_question"],
                    "outcome_name": selected["outcome_name"],
                    "normalized_outcome_label": selected["normalized_outcome_label"],
                    "snapshot_label": snapshot_label,
                    "target_date": target_date.isoformat() if target_date else "",
                    "selected_timestamp": selected["timestamp"],
                    "selected_datetime_utc": selected["datetime_utc"],
                    "selected_date": selected["date"],
                    "probability": probability,
                    "realized_outcome_0_1": realized_0_1,
                    "brier_score": brier_score,
                    "absolute_error": absolute_error,
                    "source_endpoint_type": selected["source_endpoint_type"],
                    "source_endpoint": selected["source_endpoint"],
                    "raw_file_path": selected["raw_file_path"],
                    "data_status": selected["data_status"],
                    "notes": f"snapshot_selection={selection_mode}",
                }
            )
    return snapshot_rows


def _event_label_map() -> dict[str, str]:
    return {label: display for label, display in TARGET_LABELS.items()}


def _meeting_sort_key(meeting_month: str) -> tuple[int, int]:
    try:
        y, m = meeting_month.split("-")
        return int(y), int(m)
    except Exception:
        return (9999, 99)


def _final_probability_rows(snapshot_rows: list[dict[str, Any]], comparator_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    realized_map = {row["meeting_month"]: row["realized_outcome_label"] for row in comparator_rows}
    by_meeting: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in snapshot_rows:
        if row["snapshot_label"] == "final_available" and row["realized_outcome_0_1"] != "":
            by_meeting[row["meeting_month"]].append(row)
    out: list[dict[str, Any]] = []
    for meeting_month, rows in sorted(by_meeting.items(), key=lambda item: _meeting_sort_key(item[0])):
        realized_label = realized_map.get(meeting_month, "")
        chosen = next((row for row in rows if row["normalized_outcome_label"] == realized_label), None)
        if chosen is None:
            continue
        out.append(
            {
                "meeting_month": meeting_month,
                "meeting_date": chosen["meeting_date"],
                "event_slug": chosen["event_slug"],
                "event_title": chosen["event_title"],
                "normalized_outcome_label": chosen["normalized_outcome_label"],
                "probability": chosen["probability"],
                "realized_outcome_0_1": chosen["realized_outcome_0_1"],
                "brier_score": chosen["brier_score"],
                "absolute_error": chosen["absolute_error"],
            }
        )
    return out


def _save_backend_pair(output_base: Path, backend_png: _ChartBackend, backend_svg: _ChartBackend) -> None:
    ensure_dir(output_base.parent)
    backend_png.save(output_base.with_suffix(".png"))
    backend_svg.save(output_base.with_suffix(".svg"))


def _draw_panel_label(backend: _ChartBackend, x: float, y: float, lines: list[str], fonts: dict[str, Any], width: int) -> None:
    text = "\n".join(lines)
    backend.rect((x, y, x + width, y + 58), fill="white", outline="#d0d4da", width=1, rx=8)
    backend.multiline_text((x + 10, y + 8), text, fonts["small"], fill="#30343a", spacing=4)


def _plot_probability_paths(combined_rows: list[dict[str, Any]], output_dir: Path) -> None:
    event_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in combined_rows:
        if row["use_in_probability_analysis"] == "TRUE":
            event_rows[row["meeting_month"]].append(row)
    event_months = [meeting_month for meeting_month, rows in sorted(event_rows.items(), key=lambda item: _meeting_sort_key(item[0])) if rows]
    if not event_months:
        return

    panel_height = 320
    width = 1500
    height = panel_height * len(event_months) + 90
    left, right = 100, 1090
    legend_left = 1140
    colors = {
        "50_plus_bps_cut": "#0B1F3A",
        "25_bps_cut": "#2F3E4E",
        "no_change": "#5C677D",
        "25_plus_bps_hike": "#7A7F87",
        "other": "#9AA1AA",
    }
    fonts = {
        "title": _load_font(28, bold=True),
        "body": _load_font(18),
        "small": _load_font(15),
        "tiny": _load_font(13),
    }

    backends = [_ChartBackend("png", width, height), _ChartBackend("svg", width, height)]
    for backend in backends:
        backend.rect((0, 0, width, height), fill="white", outline=None)
        backend.text((20, 24), "Polymarket Fed decision probability paths", fonts["title"], fill="#0B1F3A", anchor="la")
        backend.text((20, 58), "Days to meeting", fonts["body"], fill="#333333", anchor="la")
        backend.text((legend_left, 58), "Series", fonts["body"], fill="#0B1F3A", anchor="la")

        for idx, meeting_month in enumerate(event_months):
            rows = sorted(event_rows[meeting_month], key=lambda row: _parse_datetime(row["datetime_utc"]) or datetime.min.replace(tzinfo=timezone.utc))
            panel_top = 90 + idx * panel_height
            panel_bottom = panel_top + 180
            panel_note_top = panel_top + 210
            _draw_axes(backend, left, panel_top, right, panel_bottom)
            day_offsets: list[int] = []
            for row in rows:
                day = _parse_date(row["date"])
                meeting_date = _parse_date(row["meeting_date"])
                if day is None or meeting_date is None:
                    continue
                day_offsets.append((meeting_date - day).days)
            if day_offsets:
                x_min = min(day_offsets)
                x_max = max(day_offsets)
            else:
                x_min, x_max = 0, 1
            if x_min == x_max:
                x_max += 1

            def x_to_px(x: int) -> float:
                return left + ((x - x_min) / float(x_max - x_min)) * (right - left)

            def y_to_px(y: float) -> float:
                y = max(0.0, min(1.0, y))
                return panel_bottom - y * (panel_bottom - panel_top)

            for tick in [0.0, 0.25, 0.5, 0.75, 1.0]:
                y = y_to_px(tick)
                backend.text((left - 12, y), f"{tick:.2f}".rstrip("0").rstrip("."), fonts["tiny"], fill="#666666", anchor="ra")

            tick_candidates = [x_min, x_min + (x_max - x_min) / 2.0, x_max]
            for tick in tick_candidates:
                label = f"{int(round(tick))}"
                backend.text((x_to_px(tick), panel_bottom + 12), label, fonts["tiny"], fill="#666666", anchor="ma")

            labels = sorted({row["normalized_outcome_label"] for row in rows}, key=lambda label: DISPLAY_ORDER.index(label) if label in DISPLAY_ORDER else 99)
            legend_y = panel_top
            for label in labels:
                label_rows = [row for row in rows if row["normalized_outcome_label"] == label]
                series: list[tuple[int, float]] = []
                for row in label_rows:
                    day = _parse_date(row["date"])
                    meeting_date = _parse_date(row["meeting_date"])
                    probability = _safe_float(row["probability"])
                    if day is None or meeting_date is None or probability is None:
                        continue
                    series.append(((meeting_date - day).days, probability))
                if not series:
                    continue
                series.sort(key=lambda item: item[0])
                pts = [(x_to_px(x), y_to_px(y)) for x, y in series]
                for pos in range(len(pts) - 1):
                    backend.line(pts[pos], pts[pos + 1], colors.get(label, "#5C677D"), 3)
                for pt in pts:
                    backend.circle(pt, 3.5, fill=colors.get(label, "#5C677D"), outline="white", width=1)
                backend.line((legend_left, legend_y + 9), (legend_left + 28, legend_y + 9), colors.get(label, "#5C677D"), 4)
                backend.circle((legend_left + 14, legend_y + 9), 4, fill=colors.get(label, "#5C677D"), outline="white", width=1)
                backend.text((legend_left + 40, legend_y + 9), TARGET_LABELS.get(label, label), fonts["small"], fill="#30343A", anchor="la")
                legend_y += 28

            meeting_date = _parse_date(rows[0]["meeting_date"])
            if meeting_date is not None:
                zero_x = x_to_px(0)
                backend.line((zero_x, panel_top), (zero_x, panel_bottom), "#B1B7C2", 1)

            realized_label = REALIZED_DECISIONS.get(meeting_month, {}).get("realized_outcome_label", "")
            if realized_label:
                backend.text((right - 10, panel_top + 12), f"Realized: {TARGET_LABELS.get(realized_label, realized_label)}", fonts["small"], fill="#0B1F3A", anchor="ra")

            method_note = "Trade-based fallback series" if any(row["source_endpoint_type"] == "trades_fallback" for row in rows) else "Prices-history series"
            if meeting_month == "2026-06":
                method_note = "Prices-history series; ongoing market."
            _draw_panel_label(
                backend,
                left,
                panel_note_top,
                [meeting_month, method_note],
                fonts,
                520,
            )
    _save_backend_pair(output_dir / "fed_decisions_probability_paths", backends[0], backends[1])


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
    backends = [_ChartBackend("png", width, height), _ChartBackend("svg", width, height)]
    fonts = {
        "title": _load_font(28, bold=True),
        "body": _load_font(18),
        "small": _load_font(15),
        "tiny": _load_font(13),
    }
    colors = ["#0B1F3A", "#2F3E4E", "#5C677D", "#7A7F87", "#9AA1AA"]
    horizon_positions = {label: idx for idx, label in enumerate(SNAPSHOT_HORIZONS)}

    for backend in backends:
        backend.rect((0, 0, width, height), fill="white", outline=None)
        backend.text((20, 24), "Snapshot accuracy by meeting and horizon", fonts["title"], fill="#0B1F3A", anchor="la")
        backend.text((20, 58), "Brier score", fonts["body"], fill="#333333", anchor="la")
        _draw_axes(backend, left, top, right, bottom)
        for tick in [0.0, 0.25, 0.5, 0.75, 1.0]:
            y = bottom - (bottom - top) * tick
            backend.text((left - 12, y), f"{tick:.2f}".rstrip("0").rstrip("."), fonts["tiny"], fill="#666666", anchor="ra")
        x_slots = list(range(len(SNAPSHOT_HORIZONS)))
        slot_width = (right - left) / float(max(len(SNAPSHOT_HORIZONS), 1))
        for idx, label in enumerate(SNAPSHOT_HORIZONS):
            x = left + idx * slot_width + slot_width / 2.0
            backend.text((x, bottom + 12), label, fonts["tiny"], fill="#666666", anchor="ma")
        bar_width = slot_width / max(len(meeting_months) + 1, 2)
        for meeting_idx, meeting_month in enumerate(meeting_months):
            series = sorted(by_meeting[meeting_month], key=lambda row: horizon_positions.get(row["snapshot_label"], 99))
            for row in series:
                horizon_idx = horizon_positions.get(row["snapshot_label"])
                if horizon_idx is None:
                    continue
                probability = _safe_float(row["brier_score"])
                if probability is None:
                    continue
                center = left + horizon_idx * slot_width + slot_width / 2.0
                x0 = center - ((len(meeting_months) * bar_width) / 2.0) + meeting_idx * bar_width
                x1 = x0 + bar_width * 0.82
                y1 = bottom
                y0 = bottom - probability * (bottom - top)
                backend.rect((x0, y0, x1, y1), fill=colors[meeting_idx % len(colors)], outline="#ffffff", width=1)
        legend_y = top
        for meeting_idx, meeting_month in enumerate(meeting_months):
            backend.rect((legend_left, legend_y, legend_left + 16, legend_y + 16), fill=colors[meeting_idx % len(colors)], outline="#ffffff", width=1)
            backend.text((legend_left + 24, legend_y + 8), meeting_month, fonts["small"], fill="#30343A", anchor="la")
            legend_y += 26
        backend.text((legend_left, legend_y + 8), "Missing horizons are omitted.", fonts["small"], fill="#4A4F57", anchor="la")
        backend.text((left, 540), "Only horizons supported by the local cache are shown.", fonts["small"], fill="#4A4F57", anchor="la")

    _save_backend_pair(output_dir / "fed_decisions_snapshot_accuracy", backends[0], backends[1])


def _plot_final_probability_vs_outcome(final_rows: list[dict[str, Any]], output_dir: Path) -> None:
    if not final_rows:
        return
    rows = sorted(final_rows, key=lambda row: _meeting_sort_key(row["meeting_month"]))
    width, height = 1080, 560
    left, top, right, bottom = 100, 100, 920, 420
    legend_left = 900
    backends = [_ChartBackend("png", width, height), _ChartBackend("svg", width, height)]
    fonts = {
        "title": _load_font(28, bold=True),
        "body": _load_font(18),
        "small": _load_font(15),
        "tiny": _load_font(13),
    }
    colors = ["#0B1F3A", "#2F3E4E", "#5C677D", "#7A7F87", "#9AA1AA"]
    for backend in backends:
        backend.rect((0, 0, width, height), fill="white", outline=None)
        backend.text((20, 24), "Final probability assigned to the realized Fed outcome", fonts["title"], fill="#0B1F3A", anchor="la")
        backend.text((20, 58), "Final pre-meeting probability", fonts["body"], fill="#333333", anchor="la")
        _draw_axes(backend, left, top, right, bottom)
        for tick in [0.0, 0.25, 0.5, 0.75, 1.0]:
            y = bottom - (bottom - top) * tick
            backend.text((left - 12, y), f"{tick:.2f}".rstrip("0").rstrip("."), fonts["tiny"], fill="#666666", anchor="ra")
        slot_width = (right - left) / float(len(rows))
        for idx, row in enumerate(rows):
            center = left + idx * slot_width + slot_width / 2.0
            prob = float(row["probability"])
            x0 = center - slot_width * 0.25
            x1 = center + slot_width * 0.25
            y0 = bottom - prob * (bottom - top)
            backend.rect((x0, y0, x1, bottom), fill=colors[idx % len(colors)], outline="#ffffff", width=1)
            backend.text((center, bottom + 12), row["meeting_month"], fonts["tiny"], fill="#666666", anchor="ma")
            backend.text((center, y0 - 8), f"{prob:.3f}", fonts["tiny"], fill="#111111", anchor="ma")
        backend.line((left, top), (right, top), "#e6e6e6", 1)
        backend.text((legend_left, top), "Realized meetings", fonts["body"], fill="#0B1F3A", anchor="la")
        legend_y = top + 32
        for idx, row in enumerate(rows):
            backend.rect((legend_left, legend_y, legend_left + 16, legend_y + 16), fill=colors[idx % len(colors)], outline="#ffffff", width=1)
            backend.text((legend_left + 24, legend_y + 8), row["meeting_month"], fonts["small"], fill="#30343A", anchor="la")
            legend_y += 26
        backend.text((left, 456), "Bars are shown only where a realized outcome and a usable pre-meeting probability series are both available.", fonts["small"], fill="#4A4F57", anchor="la")

    _save_backend_pair(output_dir / "fed_decisions_final_probability_vs_outcome", backends[0], backends[1])


def _write_memo(base_dir: Path, audit_rows: list[dict[str, Any]], comparator_rows: list[dict[str, Any]], combined_rows: list[dict[str, Any]], daily_rows: list[dict[str, Any]], snapshot_rows: list[dict[str, Any]]) -> Path:
    notes_dir = ensure_dir(base_dir / "analysis" / "notes")
    memo_path = notes_dir / "fed_multi_event_analysis.md"
    accepted = [row for row in audit_rows if row["accepted_true_false"] == "TRUE"]
    usable_events = sorted({row["meeting_month"] for row in combined_rows})
    resolved_events = [row["meeting_month"] for row in comparator_rows]
    traded = any(row["source_endpoint_type"] == "trades_fallback" for row in combined_rows)
    priced = any(row["source_endpoint_type"] == "prices_history" for row in combined_rows)
    resolved_count = len(resolved_events)
    usable_count = len(usable_events)
    memo = [
        "# Fed Multi-Event Analysis Status",
        "",
        "## Current Status",
        f"- Candidate audit completed for {len(audit_rows)} exact or near-exact Fed decision markets.",
        f"- Exact accepted markets: {', '.join(row['event_month'] for row in accepted if row['accepted_true_false'] == 'TRUE')}.",
        f"- Usable analysis events in the current build: {', '.join(usable_events) if usable_events else 'none'}.",
        f"- Realized comparator rows written for {resolved_count} meeting months.",
        f"- Source endpoint mix in the usable data: {'prices_history' if priced else 'none'}{', ' if priced and traded else ''}{'trades_fallback' if traded else ''}.",
        "",
        "## Thesis-Ready Figures",
        "- Probability path figure is ready, but it should be read as a mixed-resolution archive: January is trade-based and resolved, while June is a pre-meeting prices-history path.",
        "- Snapshot accuracy is only weakly identified at present because the local cache contains almost no supported T-minus horizons beyond final-available snapshots.",
        "- Final probability vs outcome is currently only informative for the January 2026 meeting.",
        "",
        "## Caveats",
        "- December 2025 is an exact realized market, but the local prices-history cache only contains invalid-filter error payloads, so no usable December probability series could be rebuilt from the current repository state.",
        "- July 2026 and September 2026 are exact markets but have no usable local history rows in the cache.",
        "- The current build therefore does not yet deliver the ideal repeated realized-meeting panel implied by the research question.",
        "",
        "## External Sources Still Needed",
        "- A recovered December 2025 price-history or trade cache would allow the realized December meeting to join the convergence analysis.",
        "- Once the thesis moves beyond internal Polymarket probabilities, benchmark comparators such as CME FedWatch can be added separately.",
        "",
        "## Why the June Case Still Matters",
        "The repeated Fed-decision setting is still useful even with incomplete cache coverage because it shows how a meeting-specific prediction market evolves into a probability path over time. The core thesis issue is actionability: an investor or analyst cares not only about the eventual resolved outcome, but also about when the market probability becomes high enough to justify a decision. In that sense, the June 2026 market is a live test of whether Polymarket translates meeting-specific expectations into an actionable signal before the FOMC date.",
        "",
    ]
    memo_path.write_text("\n".join(memo), encoding="utf-8")
    return memo_path


def run_fed_multi_event_analysis(base_dir: Any) -> Dict[str, Path]:
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

    audit_rows, accepted_event_slugs = _build_audit_rows(data_dir)

    all_local_rows: list[dict[str, Any]] = []
    for spec in FED_CANDIDATES:
        if spec.event_slug not in accepted_event_slugs:
            continue
        rows, _, _, _ = _load_local_event_rows(data_dir, spec)
        all_local_rows.extend(rows)

    combined_rows = _build_combined_rows(all_local_rows, accepted_event_slugs)
    comparator_rows = _build_comparator_rows()
    daily_rows = _build_daily_rows(combined_rows)
    snapshot_rows = _build_snapshot_rows(combined_rows, comparator_rows)
    final_rows = _final_probability_rows(snapshot_rows, comparator_rows)

    audit_path = debug_dir / "fed_decision_event_audit.csv"
    write_csv(audit_path, AUDIT_COLUMNS, audit_rows)

    combined_path = cleaned_dir / "polymarket_fed_decisions_multi_event_long.csv"
    write_csv(combined_path, COMBINED_LONG_COLUMNS, combined_rows)

    comparator_path = comparator_dir / "fed_realized_decisions.csv"
    write_csv(comparator_path, COMPARATOR_COLUMNS, comparator_rows)

    daily_path = daily_dir / "fed_decisions_multi_event_daily.csv"
    write_csv(daily_path, DAILY_COLUMNS, daily_rows)

    snapshot_path = snapshots_dir / "fed_decisions_multi_event_snapshots.csv"
    write_csv(snapshot_path, SNAPSHOT_COLUMNS, snapshot_rows)

    _plot_probability_paths(combined_rows, figures_dir)
    _plot_snapshot_accuracy(snapshot_rows, figures_dir)
    _plot_final_probability_vs_outcome(final_rows, figures_dir)

    memo_path = _write_memo(data_dir, audit_rows, comparator_rows, combined_rows, daily_rows, snapshot_rows)

    return {
        "audit": audit_path,
        "combined_long": combined_path,
        "comparator": comparator_path,
        "daily": daily_path,
        "snapshots": snapshot_path,
        "figures": figures_dir,
        "notes": memo_path,
    }
