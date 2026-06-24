"""Focused Polymarket-only collection for a single event.

This module is intentionally narrow. It resolves one Polymarket event,
extracts exact CLOB token IDs, downloads price history, and writes a clean
long-form CSV of historical probabilities.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .collectors import HttpClient, RawArtifactWriter
from .config import project_paths
from .io_utils import ensure_dir, write_csv


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com"
OUTPUT_COLUMNS = (
    "timestamp",
    "datetime_utc",
    "date",
    "event_slug",
    "event_title",
    "market_slug",
    "market_question",
    "condition_id",
    "token_id",
    "outcome_name",
    "price",
    "probability",
    "volume",
    "liquidity",
    "source_category",
    "source_name",
    "platform",
    "source_endpoint",
    "raw_file_path",
    "data_status",
    "use_in_probability_analysis",
)


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _first_nonempty(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(_as_text(item) for item in value if _as_text(item))
    if isinstance(value, dict):
        for key in ("title", "name", "slug", "question", "description"):
            if value.get(key):
                return _as_text(value[key])
    return str(value)


def _parse_json_array(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_first_nonempty(item) for item in value if _first_nonempty(item)]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return [_first_nonempty(value)]
        if isinstance(parsed, list):
            return [_first_nonempty(item) for item in parsed if _first_nonempty(item)]
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


def _parse_timestamp(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, str) and value.endswith("Z"):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except Exception:
            return None
    try:
        return int(float(value))
    except Exception:
        return None


def _format_datetime_utc(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


@dataclass
class PolymarketResolvedEvent:
    event_url: str
    event_slug: str
    event_title: str
    raw_event_path: str
    markets: list[dict[str, Any]]
    raw_event: dict[str, Any]


@dataclass
class PolymarketTokenSpec:
    event_slug: str
    event_title: str
    market_slug: str
    market_question: str
    condition_id: str
    token_id: str
    outcome_name: str
    raw_outcome_name: str
    volume: str
    liquidity: str
    closed: str
    resolved_flag: str
    source_endpoint: str
    market_url: str


@dataclass
class PolymarketEventSpec:
    event_url: str
    output_stem: str
    token_audit_filename: str
    history_failures_filename: str
    selected_token_label: str
    token_selector: Callable[[PolymarketTokenSpec], bool]


class PolymarketResolver:
    def __init__(self, client: HttpClient, writer: RawArtifactWriter):
        self.client = client
        self.writer = writer

    def resolve(self, event_url: str) -> PolymarketResolvedEvent:
        slug = self._slug_from_url(event_url)
        endpoint = f"{GAMMA_BASE}/events"
        params = {"slug": slug}
        response, payload = self.client.get_json(endpoint, params=params)
        raw_path = self.writer.write_json("polymarket", f"event_{_slugify(slug)}", payload if payload is not None else {"raw_text": response.text})
        if not response.ok or payload is None:
            raise RuntimeError(f"Polymarket event resolution failed for {slug}: HTTP {response.status_code}")

        event = self._select_event(payload, slug)
        if event is None:
            raise RuntimeError(f"Polymarket event resolution returned no exact match for {slug}")

        markets = event.get("markets") if isinstance(event.get("markets"), list) else []
        return PolymarketResolvedEvent(
            event_url=event_url,
            event_slug=_first_nonempty(event.get("slug"), slug),
            event_title=_first_nonempty(event.get("title"), event.get("name"), event.get("question"), slug),
            raw_event_path=raw_path,
            markets=[market for market in markets if isinstance(market, dict)],
            raw_event=event,
        )

    def _slug_from_url(self, event_url: str) -> str:
        path = urlparse(event_url).path.strip("/")
        if not path:
            return ""
        return path.split("/")[-1]

    def _select_event(self, payload: Any, slug: str) -> dict[str, Any] | None:
        if isinstance(payload, dict):
            if _first_nonempty(payload.get("slug")) == slug:
                return payload
            if isinstance(payload.get("markets"), list):
                return payload
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and _first_nonempty(item.get("slug")) == slug:
                    return item
            for item in payload:
                if isinstance(item, dict) and isinstance(item.get("markets"), list):
                    return item
        return None


class PolymarketTokenExtractor:
    def __init__(self, paths: dict[str, Path], writer: RawArtifactWriter):
        self.paths = paths
        self.writer = writer

    def extract(
        self,
        resolved: PolymarketResolvedEvent,
        audit_filename: str,
        token_selector: Callable[[PolymarketTokenSpec], bool] | None = None,
    ) -> tuple[list[PolymarketTokenSpec], str]:
        rows: list[dict[str, Any]] = []
        tokens: list[PolymarketTokenSpec] = []
        for market in resolved.markets:
            market_slug = _first_nonempty(market.get("slug"), market.get("id"), market.get("question"))
            market_question = _first_nonempty(market.get("question"), market.get("title"), market.get("name"), market_slug)
            condition_id = _first_nonempty(market.get("conditionId"), market.get("condition_id"), market.get("conditionID"))
            token_ids = _parse_json_array(market.get("clobTokenIds") or market.get("clob_token_ids") or market.get("tokenIds") or market.get("token_ids"))
            outcomes = self._extract_outcomes(market)
            volume = _first_nonempty(market.get("volume"), market.get("volumeNum"), market.get("volume24hr"))
            liquidity = _first_nonempty(market.get("liquidity"), market.get("liquidityNum"), market.get("liquidityClob"), market.get("totalLiquidity"))
            closed = _first_nonempty(market.get("closed"), market.get("archived"), market.get("active"))
            resolved_flag = _first_nonempty(market.get("automaticallyResolved"), market.get("resolvedBy"), market.get("umaResolutionStatus"))
            market_url = _first_nonempty(market.get("url"), f"https://polymarket.com/event/{resolved.event_slug}")

            for index, token_id in enumerate(token_ids):
                outcome_name = outcomes[index] if index < len(outcomes) else (outcomes[0] if outcomes else "")
                raw_outcome_name = outcome_name
                if (
                    resolved.event_slug == "presidential-election-winner-2024"
                    and "trump" in market_question.lower()
                    and raw_outcome_name.lower() == "yes"
                ):
                    outcome_name = "Donald Trump"
                token = PolymarketTokenSpec(
                    event_slug=resolved.event_slug,
                    event_title=resolved.event_title,
                    market_slug=market_slug,
                    market_question=market_question,
                    condition_id=condition_id,
                    token_id=token_id,
                    outcome_name=outcome_name,
                    raw_outcome_name=raw_outcome_name,
                    volume=volume,
                    liquidity=liquidity,
                    closed=closed,
                    resolved_flag=resolved_flag,
                    source_endpoint=f"{GAMMA_BASE}/events?slug={resolved.event_slug}",
                    market_url=market_url,
                )
                tokens.append(token)
                rows.append(
                    {
                        "event_slug": token.event_slug,
                        "event_title": token.event_title,
                        "market_slug": token.market_slug,
                        "market_question": token.market_question,
                        "condition_id": token.condition_id,
                        "clob_token_ids": json.dumps(token_ids),
                        "token_id": token.token_id,
                        "outcome_name": token.outcome_name,
                        "raw_outcome_name": token.raw_outcome_name,
                        "volume": token.volume,
                        "liquidity": token.liquidity,
                        "closed": token.closed,
                        "resolved": token.resolved_flag,
                        "market_url": token.market_url,
                        "source_endpoint": token.source_endpoint,
                        "selected_for_history": "TRUE" if token_selector and token_selector(token) else "FALSE",
                    }
                )

        audit_path = self.paths["debug"] / audit_filename
        write_csv(
            audit_path,
            [
                "event_slug",
                "event_title",
                "market_slug",
                "market_question",
                "condition_id",
                "clob_token_ids",
                "token_id",
                "outcome_name",
                "raw_outcome_name",
                "volume",
                "liquidity",
                "closed",
                "resolved",
                "market_url",
                "source_endpoint",
                "selected_for_history",
            ],
            rows,
        )
        return tokens, str(audit_path)

    def _extract_outcomes(self, market: dict[str, Any]) -> list[str]:
        for key in ("outcomes", "tokens"):
            value = market.get(key)
            if isinstance(value, list):
                outcomes = [_as_text(item) for item in value if _as_text(item)]
                if outcomes:
                    return outcomes
            if isinstance(value, str) and value.strip():
                outcomes = _parse_json_array(value)
                if outcomes:
                    return outcomes
        return []


class PolymarketHistoryFetcher:
    def __init__(self, client: HttpClient, writer: RawArtifactWriter):
        self.client = client
        self.writer = writer

    def fetch(self, resolved: PolymarketResolvedEvent, tokens: list[PolymarketTokenSpec]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
        rows: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        stats = {"attempted": 0, "non_empty": 0}

        for token in tokens:
            success = False
            for interval in ("1d", "1h", "6h"):
                endpoint = f"{CLOB_BASE}/prices-history"
                params = {"market": token.token_id, "interval": interval}
                stats["attempted"] += 1
                response, payload = self.client.get_json(endpoint, params=params)
                raw_path = self.writer.write_text("polymarket", f"prices_history_{_slugify(token.token_id)}_{interval}", response.text)
                history_rows = _extract_history_rows(payload)
                print(
                    "Polymarket history:",
                    {
                        "endpoint": f"{endpoint}?market={token.token_id}&interval={interval}",
                        "status_code": response.status_code,
                        "token_id": token.token_id,
                        "outcome_name": token.outcome_name,
                        "history_points": len(history_rows),
                    },
                )
                if response.ok and history_rows:
                    stats["non_empty"] += 1
                    rows.extend(self._history_rows_to_output(resolved, token, history_rows, f"{endpoint}?market={token.token_id}&interval={interval}", raw_path))
                    success = True
                    break
            if not success:
                trade_rows = self._fetch_trade_history(token, result=None, stats=stats)
                if trade_rows:
                    stats["non_empty"] += 1
                    rows.extend(self._trade_rows_to_output(resolved, token, trade_rows))
                else:
                    failures.append(
                        {
                            "event_slug": resolved.event_slug,
                            "market_slug": token.market_slug,
                            "market_question": token.market_question,
                            "token_id": token.token_id,
                            "outcome_name": token.outcome_name,
                            "source_endpoint": f"{CLOB_BASE}/prices-history?market={token.token_id}",
                            "reason": "no_history_rows_returned",
                        }
                    )

        return rows, failures, stats

    def _fetch_trade_history(
        self,
        token: PolymarketTokenSpec,
        result: CollectionResult | None,
        stats: dict[str, int] | None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        offset = 0
        limit = 1000
        while True:
            endpoint = f"{DATA_BASE}/trades"
            params = {"market": token.condition_id, "limit": limit, "offset": offset}
            if stats is not None:
                stats["attempted"] += 1
            response, payload = self.client.get_json(endpoint, params=params)
            raw_path = self.writer.write_text("polymarket", f"trades_{_slugify(token.token_id)}_{offset}", response.text)
            if result is not None:
                result.raw_files.append(raw_path)
                result.attempted_endpoints.append(f"{endpoint}?{params}")
                result.audit.append({"endpoint": endpoint, "params": params, "status_code": response.status_code, "token_id": token.token_id, "condition_id": token.condition_id})
            trade_rows = [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
            filtered = [row for row in trade_rows if _first_nonempty(row.get("asset")) == token.token_id]
            print(
                "Polymarket trades fallback:",
                {
                    "endpoint": f"{endpoint}?market={token.condition_id}&limit={limit}&offset={offset}",
                    "status_code": response.status_code,
                    "token_id": token.token_id,
                    "outcome_name": token.outcome_name,
                    "trade_points": len(filtered),
                },
            )
            if response.ok and filtered:
                for row in filtered:
                    row = dict(row)
                    row["__raw_path__"] = raw_path
                    row["__endpoint__"] = f"{endpoint}?market={token.condition_id}&limit={limit}&offset={offset}"
                    rows.append(row)
            if not response.ok or len(trade_rows) < limit:
                break
            offset += limit
        return rows

    def _trade_rows_to_output(self, resolved: PolymarketResolvedEvent, token: PolymarketTokenSpec, trade_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in trade_rows:
            timestamp = _parse_timestamp(item.get("timestamp"))
            price = _parse_float(item.get("price"))
            if timestamp is None or price is None:
                continue
            if not 0.0 <= price <= 1.0:
                continue
            rows.append(
                {
                    "timestamp": timestamp,
                    "datetime_utc": _format_datetime_utc(timestamp),
                    "date": datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(),
                    "event_slug": resolved.event_slug,
                    "event_title": resolved.event_title,
                    "market_slug": token.market_slug,
                    "market_question": token.market_question,
                    "condition_id": token.condition_id,
                    "token_id": token.token_id,
                    "outcome_name": token.outcome_name,
                    "price": price,
                    "probability": price,
                    "volume": token.volume,
                    "liquidity": token.liquidity,
                    "source_category": "prediction_market",
                    "source_name": "Polymarket",
                    "platform": "Polymarket",
                    "source_endpoint": _first_nonempty(item.get("__endpoint__"), f"{DATA_BASE}/trades?market={token.condition_id}&limit=1000"),
                    "raw_file_path": _first_nonempty(item.get("__raw_path__")),
                    "data_status": "real_api_data",
                    "use_in_probability_analysis": "TRUE",
                }
            )
        return rows

    def _history_rows_to_output(
        self,
        resolved: PolymarketResolvedEvent,
        token: PolymarketTokenSpec,
        history_rows: list[dict[str, Any]],
        source_endpoint: str,
        raw_path: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in history_rows:
            timestamp = _parse_timestamp(item.get("t") or item.get("timestamp") or item.get("time") or item.get("ts"))
            price = _parse_float(item.get("p") or item.get("price") or item.get("value"))
            if timestamp is None or price is None:
                continue
            if not 0.0 <= price <= 1.0:
                continue
            rows.append(
                {
                    "timestamp": timestamp,
                    "datetime_utc": _format_datetime_utc(timestamp),
                    "date": datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(),
                    "event_slug": resolved.event_slug,
                    "event_title": resolved.event_title,
                    "market_slug": token.market_slug,
                    "market_question": token.market_question,
                    "condition_id": token.condition_id,
                    "token_id": token.token_id,
                    "outcome_name": token.outcome_name,
                    "price": price,
                    "probability": price,
                    "volume": token.volume,
                    "liquidity": token.liquidity,
                    "source_category": "prediction_market",
                    "source_name": "Polymarket",
                    "platform": "Polymarket",
                    "source_endpoint": source_endpoint,
                    "raw_file_path": raw_path,
                    "data_status": "real_api_data",
                    "use_in_probability_analysis": "TRUE",
                }
            )
        return rows


def _selected_token_summary_label(label: str, token_count: int) -> str:
    return f"{label} found: {'yes' if token_count else 'no'}"


def run_polymarket_event(base_dir: Path | str, spec: PolymarketEventSpec) -> dict[str, Path]:
    paths = project_paths(Path(base_dir))
    ensure_dir(paths["debug"])
    writer = RawArtifactWriter(paths["raw"])
    client = HttpClient()
    resolver = PolymarketResolver(client, writer)
    extractor = PolymarketTokenExtractor(paths, writer)
    fetcher = PolymarketHistoryFetcher(client, writer)

    resolved = resolver.resolve(spec.event_url)
    all_tokens, token_audit_path = extractor.extract(resolved, spec.token_audit_filename, spec.token_selector)
    selected_tokens = [token for token in all_tokens if spec.token_selector(token)]
    rows, failures, stats = fetcher.fetch(resolved, selected_tokens)

    output_csv = paths["cleaned"] / f"{spec.output_stem}_price_history_long.csv"
    write_csv(output_csv, OUTPUT_COLUMNS, rows)

    failures_path = paths["debug"] / spec.history_failures_filename
    write_csv(
        failures_path,
        ["event_slug", "market_slug", "market_question", "token_id", "outcome_name", "source_endpoint", "reason"],
        failures,
    )

    if not rows:
        raise RuntimeError(f"Polymarket {spec.output_stem} history fetch completed with zero output rows.")

    print("Focused Polymarket summary:")
    print("event resolved: yes")
    print(f"markets found: {len(resolved.markets)}")
    print(f"token IDs found: {len(all_tokens)}")
    print(f"history requests attempted: {stats['attempted']}")
    print(f"non-empty history responses: {stats['non_empty']}")
    print(f"rows written to CSV: {len(rows)}")
    print(f"output CSV path: {output_csv}")
    print(_selected_token_summary_label(spec.selected_token_label, len(selected_tokens)))

    return {
        "output_csv": output_csv,
        "token_audit": Path(token_audit_path),
        "history_failures": failures_path,
        "raw_event": Path(resolved.raw_event_path),
    }


def _presidential_winner_trump_selector(token: PolymarketTokenSpec) -> bool:
    text = " ".join(part for part in [token.market_slug, token.market_question, token.outcome_name] if part).lower()
    if any(bad in text for bad in ("popular vote", "state", "margin", "claims victory", "victory by", "electoral college by state")):
        return False
    if token.raw_outcome_name.lower() != "yes":
        return False
    return "trump" in text and ("presidential election winner" in text or "winner 2024" in text or "2024 us presidential election" in text or "president" in text)


def _any_token_selector(token: PolymarketTokenSpec) -> bool:
    return True


def polymarket_event_spec(event_key: str) -> PolymarketEventSpec:
    if event_key == "anthropic_valuation":
        return PolymarketEventSpec(
            event_url="https://polymarket.com/event/will-anthropics-valuation-hit-by-june-30",
            output_stem="polymarket_anthropic",
            token_audit_filename="polymarket_anthropic_token_audit.csv",
            history_failures_filename="polymarket_anthropic_history_failures.csv",
            selected_token_label="Anthropic token",
            token_selector=_any_token_selector,
        )
    if event_key == "trump_2024":
        return PolymarketEventSpec(
            event_url="https://polymarket.com/event/presidential-election-winner-2024",
            output_stem="polymarket_trump_2024",
            token_audit_filename="polymarket_trump_2024_token_audit.csv",
            history_failures_filename="polymarket_trump_2024_history_failures.csv",
            selected_token_label="Donald Trump token",
            token_selector=_presidential_winner_trump_selector,
        )
    if event_key == "fed_january":
        return PolymarketEventSpec(
            event_url="https://polymarket.com/event/fed-decision-in-january",
            output_stem="polymarket_fed_january",
            token_audit_filename="polymarket_fed_january_token_audit.csv",
            history_failures_filename="polymarket_fed_january_history_failures.csv",
            selected_token_label="decision token",
            token_selector=_any_token_selector,
        )
    raise ValueError(f"Unsupported focused Polymarket event: {event_key}")
