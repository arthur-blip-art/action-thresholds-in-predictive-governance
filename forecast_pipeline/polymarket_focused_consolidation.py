"""Focused consolidation for the three Polymarket event datasets."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any

from .config import project_paths
from .io_utils import ensure_dir, read_csv_rows, write_csv


FOCUSED_CASES = (
    {
        "research_case": "anthropic_valuation",
        "source_csv": "polymarket_anthropic_price_history_long.csv",
        "event_slug": "will-anthropics-valuation-hit-by-june-30",
        "raw_event_slug": "will-anthropics-valuation-hit-by-june-30",
    },
    {
        "research_case": "trump_2024",
        "source_csv": "polymarket_trump_2024_price_history_long.csv",
        "event_slug": "presidential-election-winner-2024",
        "raw_event_slug": "presidential-election-winner-2024",
    },
    {
        "research_case": "fed_january",
        "source_csv": "polymarket_fed_january_price_history_long.csv",
        "event_slug": "fed-decision-in-january",
        "raw_event_slug": "fed-decision-in-january",
    },
)

UNIFIED_COLUMNS = (
    "timestamp",
    "datetime_utc",
    "date",
    "research_case",
    "event_slug",
    "event_title",
    "market_slug",
    "market_question",
    "condition_id",
    "token_id",
    "outcome_name",
    "price",
    "probability",
    "source_category",
    "source_name",
    "platform",
    "source_endpoint",
    "source_endpoint_type",
    "raw_file_path",
    "data_status",
    "use_in_probability_analysis",
    "final_outcome_0_1",
    "resolution_status",
    "notes",
)

DAILY_COLUMNS = (
    "research_case",
    "event_slug",
    "event_title",
    "market_slug",
    "market_question",
    "condition_id",
    "token_id",
    "outcome_name",
    "date",
    "source_endpoint_type",
    "first_probability",
    "last_probability",
    "mean_probability",
    "min_probability",
    "max_probability",
    "number_of_observations",
    "source_category",
    "source_name",
    "platform",
    "source_endpoints",
    "raw_file_paths",
    "data_status",
    "use_in_probability_analysis",
    "final_outcome_0_1",
    "resolution_status",
    "notes",
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
    "source_category",
    "source_name",
    "platform",
    "source_endpoint",
    "raw_file_path",
    "data_status",
    "use_in_probability_analysis",
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
    dt = _parse_datetime(value)
    if dt is not None:
        return dt.date()
    try:
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


def _safe_float(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _source_endpoint_type(source_endpoint: str) -> str:
    if "data-api.polymarket.com/trades" in source_endpoint:
        return "trades_fallback"
    return "prices_history"


def _latest_file(patterns: list[Path]) -> Path | None:
    if not patterns:
        return None
    return max(patterns, key=lambda path: path.stat().st_mtime)


def _load_event_json(raw_dir: Path, raw_event_slug: str) -> dict[str, Any]:
    files = list(raw_dir.glob(f"*event_{raw_event_slug}.json"))
    latest = _latest_file(files)
    if latest is None:
        raise FileNotFoundError(f"No raw Polymarket event JSON found for {raw_event_slug}")
    payload = json.loads(latest.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        if not payload:
            raise ValueError(f"Raw Polymarket event JSON is empty for {raw_event_slug}")
        first = payload[0]
        if isinstance(first, dict):
            return first
        raise ValueError(f"Raw Polymarket event JSON has unexpected shape for {raw_event_slug}")
    if not isinstance(payload, dict):
        raise ValueError(f"Raw Polymarket event JSON has unexpected shape for {raw_event_slug}")
    return payload


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
    token_ids = _parse_json_list(market.get("clobTokenIds") or market.get("clob_token_ids") or market.get("tokenIds") or market.get("token_ids"))
    outcome_prices = _parse_json_list(market.get("outcomePrices"))
    final_map: dict[str, str] = {}
    for token_id, outcome_price in zip(token_ids, outcome_prices):
        value = _safe_float(outcome_price)
        if value is None:
            continue
        if value >= 0.5:
            final_map[token_id] = "1"
        elif value <= 0.5:
            final_map[token_id] = "0"
    return final_map


@dataclass
class EventContext:
    research_case: str
    event_slug: str
    event_title: str
    event_date: date | None
    resolution_status: str
    market_final_outcomes: dict[str, str]
    event_raw_path: str


def _load_event_context(paths: dict[str, Path], spec: dict[str, str]) -> EventContext:
    raw_dir = paths["raw"] / "polymarket"
    payload = _load_event_json(raw_dir, spec["raw_event_slug"])
    event_date = _parse_date(str(payload.get("endDate") or payload.get("endDateIso") or ""))
    event_closed = bool(payload.get("closed") or payload.get("ended"))
    resolution_status = "resolved" if event_closed else "open"
    market_final_outcomes: dict[str, str] = {}
    markets = payload.get("markets") if isinstance(payload.get("markets"), list) else []
    for market in markets:
        if not isinstance(market, dict):
            continue
        market_slug = str(market.get("slug") or "").strip()
        for token_id, final_outcome in _market_final_outcomes(market, event_closed).items():
            market_final_outcomes[token_id] = final_outcome
        # If the row is Trump-only and resolved, the selected token is the Yes token.
        if spec["research_case"] == "trump_2024" and market_slug == "will-donald-trump-win-the-2024-us-presidential-election":
            token_ids = _parse_json_list(market.get("clobTokenIds"))
            outcome_prices = _parse_json_list(market.get("outcomePrices"))
            if token_ids and outcome_prices and len(token_ids) == len(outcome_prices):
                for token_id, outcome_price in zip(token_ids, outcome_prices):
                    value = _safe_float(outcome_price)
                    if value is None:
                        continue
                    market_final_outcomes[token_id] = "1" if value >= 0.5 else "0"
    event_raw_files = sorted(raw_dir.glob(f"*event_{spec['raw_event_slug']}.json"))
    latest_event_raw = _latest_file(event_raw_files)
    event_raw_path = str(latest_event_raw) if latest_event_raw else ""
    return EventContext(
        research_case=spec["research_case"],
        event_slug=spec["event_slug"],
        event_title=str(payload.get("title") or spec["event_slug"]),
        event_date=event_date,
        resolution_status=resolution_status,
        market_final_outcomes=market_final_outcomes,
        event_raw_path=event_raw_path,
    )


def _load_source_rows(paths: dict[str, Path], spec: dict[str, str], context: EventContext) -> tuple[list[dict[str, Any]], list[str]]:
    source_csv = paths["cleaned"] / spec["source_csv"]
    rows = read_csv_rows(source_csv)
    consolidated: list[dict[str, Any]] = []
    rejected: list[str] = []
    for row in rows:
        token_id = str(row.get("token_id", "")).strip()
        source_endpoint = str(row.get("source_endpoint", "")).strip()
        raw_file_path = str(row.get("raw_file_path", "")).strip()
        if not token_id or not source_endpoint or not raw_file_path:
            rejected.append(token_id or row.get("market_question", "") or "unknown")
            continue

        outcome_name = str(row.get("outcome_name", "")).strip()
        if context.research_case == "trump_2024" and outcome_name != "Donald Trump":
            continue

        source_endpoint_type = _source_endpoint_type(source_endpoint)
        final_outcome = context.market_final_outcomes.get(token_id, "") if context.resolution_status == "resolved" else ""
        notes = [f"generated_from={source_endpoint_type}"]
        if context.research_case == "trump_2024":
            notes.append("donald_trump_only")
        if context.resolution_status == "resolved" and not final_outcome:
            notes.append("final_outcome_unknown")

        consolidated.append(
            {
                "timestamp": str(row.get("timestamp", "")).strip(),
                "datetime_utc": str(row.get("datetime_utc", "")).strip(),
                "date": str(row.get("date", "")).strip(),
                "research_case": context.research_case,
                "event_slug": str(row.get("event_slug", context.event_slug)).strip(),
                "event_title": str(row.get("event_title", context.event_title)).strip(),
                "market_slug": str(row.get("market_slug", "")).strip(),
                "market_question": str(row.get("market_question", "")).strip(),
                "condition_id": str(row.get("condition_id", "")).strip(),
                "token_id": token_id,
                "outcome_name": outcome_name,
                "price": str(row.get("price", "")).strip(),
                "probability": str(row.get("probability", "")).strip(),
                "source_category": str(row.get("source_category", "prediction_market")).strip(),
                "source_name": str(row.get("source_name", "Polymarket")).strip(),
                "platform": str(row.get("platform", "Polymarket")).strip(),
                "source_endpoint": source_endpoint,
                "source_endpoint_type": source_endpoint_type,
                "raw_file_path": raw_file_path,
                "data_status": str(row.get("data_status", "")).strip(),
                "use_in_probability_analysis": str(row.get("use_in_probability_analysis", "")).strip(),
                "final_outcome_0_1": final_outcome,
                "resolution_status": context.resolution_status,
                "notes": "; ".join(notes),
                "_timestamp_dt": _parse_datetime(str(row.get("datetime_utc", ""))) or datetime.fromtimestamp(float(row.get("timestamp")), tz=timezone.utc),
            }
        )
    return consolidated, rejected


def _sort_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            row["research_case"],
            row["event_slug"],
            row["market_slug"],
            row["outcome_name"],
            row["date"],
            row["source_endpoint_type"],
            row["token_id"],
            row["_timestamp_dt"],
        ),
    )


def _build_daily_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[
            (
                row["research_case"],
                row["market_question"],
                row["outcome_name"],
                row["date"],
                row["source_endpoint_type"],
            )
        ].append(row)

    daily_rows: list[dict[str, Any]] = []
    for (research_case, market_question, outcome_name, day, endpoint_type), bucket in sorted(buckets.items()):
        ordered = sorted(bucket, key=lambda row: row["_timestamp_dt"])
        probs = [_safe_float(row["probability"]) for row in ordered]
        probs = [p for p in probs if p is not None]
        if not probs:
            continue
        first = ordered[0]
        last = ordered[-1]
        source_endpoints = "; ".join(sorted({row["source_endpoint"] for row in bucket if row["source_endpoint"]}))
        raw_file_paths = "; ".join(sorted({row["raw_file_path"] for row in bucket if row["raw_file_path"]}))
        daily_rows.append(
            {
                "research_case": research_case,
                "event_slug": first["event_slug"],
                "event_title": first["event_title"],
                "market_slug": first["market_slug"],
                "market_question": market_question,
                "condition_id": first["condition_id"],
                "token_id": first["token_id"],
                "outcome_name": outcome_name,
                "date": day,
                "source_endpoint_type": endpoint_type,
                "first_probability": probs[0],
                "last_probability": probs[-1],
                "mean_probability": mean(probs),
                "min_probability": min(probs),
                "max_probability": max(probs),
                "number_of_observations": len(bucket),
                "source_category": first["source_category"],
                "source_name": first["source_name"],
                "platform": first["platform"],
                "source_endpoints": source_endpoints,
                "raw_file_paths": raw_file_paths,
                "data_status": first["data_status"],
                "use_in_probability_analysis": first["use_in_probability_analysis"],
                "final_outcome_0_1": first["final_outcome_0_1"],
                "resolution_status": first["resolution_status"],
                "notes": "; ".join(sorted({row["notes"] for row in bucket if row["notes"]})),
            }
        )
    return daily_rows


def _select_snapshot_row(rows: list[dict[str, Any]], target_date: date | None, allow_after_fallback: bool = True) -> tuple[dict[str, Any] | None, str]:
    if not rows:
        return None, "no_data"
    ordered = sorted(rows, key=lambda row: row["_timestamp_dt"])
    if target_date is None:
        return ordered[-1], "no_target_date"
    before_or_on = [row for row in ordered if row["_timestamp_dt"].date() <= target_date]
    if before_or_on:
        return before_or_on[-1], "before_or_on_target"
    if allow_after_fallback:
        return ordered[0], "after_target_fallback"
    return None, "no_observation_before_target"


def _build_snapshot_rows(rows: list[dict[str, Any]], contexts: dict[str, EventContext]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[(row["research_case"], row["market_slug"], row["outcome_name"])].append(row)

    snapshot_rows: list[dict[str, Any]] = []
    labels = ["first_available", "T-60", "T-30", "T-14", "T-7", "T-3", "T-1", "final_available"]
    offsets = {
        "T-60": 60,
        "T-30": 30,
        "T-14": 14,
        "T-7": 7,
        "T-3": 3,
        "T-1": 1,
    }
    for key, bucket in sorted(buckets.items()):
        research_case, market_slug, outcome_name = key
        context = contexts[research_case]
        ordered = sorted(bucket, key=lambda row: row["_timestamp_dt"])
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
                offset = offsets[label]
                target = context.event_date - timedelta(days=offset) if context.event_date else None
                selected, selection_mode = _select_snapshot_row(bucket, target)
                target_date = _format_date(target)
            if selected is None:
                continue
            notes = [selected["notes"], f"snapshot_selection={selection_mode}"]
            if selection_mode == "after_target_fallback":
                notes.append("after_target_fallback=true")
            snapshot_rows.append(
                {
                    "research_case": research_case,
                    "snapshot_label": label,
                    "target_date": target_date,
                    "selected_timestamp": int(selected["timestamp"]),
                    "selected_datetime_utc": _format_datetime_utc(selected["_timestamp_dt"]),
                    "selected_date": selected["date"],
                    "event_slug": selected["event_slug"],
                    "event_title": selected["event_title"],
                    "market_slug": market_slug,
                    "market_question": selected["market_question"],
                    "condition_id": selected["condition_id"],
                    "token_id": selected["token_id"],
                    "outcome_name": outcome_name,
                    "source_endpoint_type": selected["source_endpoint_type"],
                    "probability": selected["probability"],
                    "source_category": selected["source_category"],
                    "source_name": selected["source_name"],
                    "platform": selected["platform"],
                    "source_endpoint": selected["source_endpoint"],
                    "raw_file_path": selected["raw_file_path"],
                    "data_status": selected["data_status"],
                    "use_in_probability_analysis": selected["use_in_probability_analysis"],
                    "final_outcome_0_1": selected["final_outcome_0_1"],
                    "resolution_status": selected["resolution_status"],
                    "notes": "; ".join(part for part in notes if part),
                }
            )
    return snapshot_rows


def _validation_report(
    rows: list[dict[str, Any]],
    daily_rows: list[dict[str, Any]],
    snapshot_rows: list[dict[str, Any]],
    contexts: dict[str, EventContext],
    rejected_rows: list[str],
    failure_counts: dict[str, int],
) -> str:
    lines: list[str] = ["# Polymarket Focused Validation Report", ""]
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_case[row["research_case"]].append(row)

    lines.append("## Row Counts")
    for case in ("anthropic_valuation", "trump_2024", "fed_january"):
        case_rows = by_case.get(case, [])
        lines.append(f"- {case}: {len(case_rows)} rows")
    lines.append("")

    lines.append("## Date Ranges")
    for case, case_rows in by_case.items():
        dates = [row["date"] for row in case_rows if row["date"]]
        lines.append(f"- {case}: {min(dates)} to {max(dates)}" if dates else f"- {case}: no dates")
    lines.append("")

    lines.append("## Outcomes Included")
    for case, case_rows in by_case.items():
        outcomes = sorted({row["outcome_name"] for row in case_rows if row["outcome_name"]})
        lines.append(f"- {case}: {', '.join(outcomes) if outcomes else 'none'}")
    lines.append("")

    lines.append("## Tokens With History")
    for case in ("anthropic_valuation", "trump_2024", "fed_january"):
        token_ids = sorted({row["token_id"] for row in by_case.get(case, []) if row["source_endpoint_type"] in {"prices_history", "trades_fallback"}})
        lines.append(f"- {case}: {len(token_ids)}")
    lines.append("")

    lines.append("## Tokens Without History")
    for case in ("anthropic_valuation", "trump_2024", "fed_january"):
        lines.append(f"- {case}: {failure_counts.get(case, 0)}")
    lines.append("")

    lines.append("## Endpoint Types Used")
    for case in ("anthropic_valuation", "trump_2024", "fed_january"):
        endpoint_types = sorted({row["source_endpoint_type"] for row in by_case.get(case, [])})
        lines.append(f"- {case}: {', '.join(endpoint_types) if endpoint_types else 'none'}")
    lines.append("")

    missing_required = [
        row
        for row in rows
        if not row["token_id"] or not row["source_endpoint"] or not row["raw_file_path"]
    ]
    prob_violations = [
        row
        for row in rows
        if _safe_float(row["probability"]) is None or not 0.0 <= _safe_float(row["probability"]) <= 1.0
    ]

    lines.append("## Validation Checks")
    lines.append(f"- Rows lacking token_id/source_endpoint/raw_file_path: {'yes' if missing_required else 'no'} ({len(missing_required)})")
    lines.append(f"- Probabilities all in [0, 1]: {'yes' if not prob_violations else 'no'} ({len(prob_violations)})")
    trump_outcomes = sorted({row["outcome_name"] for row in by_case.get("trump_2024", [])})
    lines.append(f"- Trump includes only Donald Trump outcome: {'yes' if trump_outcomes == ['Donald Trump'] else 'no'}")
    lines.append("")

    if rejected_rows:
        lines.append("## Dropped Rows")
        lines.append(f"- Rows dropped because of missing required trace fields: {len(rejected_rows)}")
        lines.append("")

    lines.append("## Derived Context")
    for case, ctx in contexts.items():
        lines.append(f"- {case}: event_date={_format_date(ctx.event_date)} resolution_status={ctx.resolution_status}")
    lines.append("")

    lines.append("## Snapshot Coverage")
    for case in ("anthropic_valuation", "trump_2024", "fed_january"):
        case_snapshots = [row for row in snapshot_rows if row["research_case"] == case]
        lines.append(f"- {case}: {len(case_snapshots)} snapshot rows")
    lines.append("")

    return "\n".join(lines).strip() + "\n"


def run_polymarket_focused_consolidation(base_dir: Path | str = ".") -> dict[str, Path]:
    paths = project_paths(Path(base_dir))
    ensure_dir(paths["cleaned"])
    ensure_dir(paths["daily"])
    ensure_dir(paths["snapshots"])
    ensure_dir(paths["validation"])

    contexts: dict[str, EventContext] = {}
    unified_rows: list[dict[str, Any]] = []
    dropped_rows: list[str] = []
    failure_counts: dict[str, int] = {}

    for spec in FOCUSED_CASES:
        context = _load_event_context(paths, spec)
        contexts[spec["research_case"]] = context
        rows, rejected = _load_source_rows(paths, spec, context)
        failure_csv = paths["debug"] / spec["source_csv"].replace("_price_history_long.csv", "_history_failures.csv")
        failure_counts[spec["research_case"]] = 0
        if failure_csv.exists():
            failure_rows = read_csv_rows(failure_csv)
            failure_counts[spec["research_case"]] = len({row.get("token_id", "") for row in failure_rows if row.get("token_id")})
        dropped_rows.extend(rejected)
        unified_rows.extend(rows)

    unified_rows = _sort_rows(unified_rows)

    unified_path = paths["cleaned"] / "polymarket_focused_events_long.csv"
    write_csv(unified_path, UNIFIED_COLUMNS, [{k: v for k, v in row.items() if not k.startswith("_")} for row in unified_rows])

    daily_rows = _build_daily_rows(unified_rows)
    daily_path = paths["daily"] / "polymarket_focused_events_daily.csv"
    write_csv(daily_path, DAILY_COLUMNS, daily_rows)

    snapshot_rows = _build_snapshot_rows(unified_rows, contexts)
    snapshot_path = paths["snapshots"] / "polymarket_focused_event_snapshots.csv"
    write_csv(snapshot_path, SNAPSHOT_COLUMNS, snapshot_rows)

    validation_md = _validation_report(unified_rows, daily_rows, snapshot_rows, contexts, dropped_rows, failure_counts)
    validation_path = paths["validation"] / "polymarket_focused_validation_report.md"
    validation_path.write_text(validation_md, encoding="utf-8")

    return {
        "unified": unified_path,
        "daily": daily_path,
        "snapshots": snapshot_path,
        "validation": validation_path,
    }
