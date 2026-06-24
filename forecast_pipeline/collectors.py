"""Live data collectors for public forecast and benchmark sources."""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

import requests

from .io_utils import ensure_dir, write_csv
from .models import Record, normalized_bool, truthy


@dataclass
class CollectionResult:
    source_id: str
    source_name: str
    source_category: str
    raw_files: list[str] = field(default_factory=list)
    records: list[Record] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    attempted_endpoints: list[str] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)
    normalized_rows: int = 0

    @property
    def succeeded(self) -> bool:
        return bool(self.records or self.raw_files)


class RawArtifactWriter:
    def __init__(self, raw_dir: Path):
        self.raw_dir = ensure_dir(raw_dir)

    def write_json(self, source_id: str, kind: str, payload: Any) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.raw_dir / source_id / f"{timestamp}_{kind}.json"
        ensure_dir(path.parent)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return str(path)

    def write_text(self, source_id: str, kind: str, text: str) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.raw_dir / source_id / f"{timestamp}_{kind}.txt"
        ensure_dir(path.parent)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def write_csv(self, source_id: str, kind: str, rows: list[dict[str, Any]], fieldnames: list[str]) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = self.raw_dir / source_id / f"{timestamp}_{kind}.csv"
        ensure_dir(path.parent)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: "" if value is None else value for key, value in row.items()})
        return str(path)


class HttpClient:
    def __init__(self, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Codex forecast pipeline)",
                "Accept": "application/json,text/plain,text/html,*/*",
            }
        )
        self.timeout = timeout

    def get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        return self.session.get(url, params=params, timeout=self.timeout)

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> tuple[requests.Response, Any | None]:
        resp = self.get(url, params=params)
        try:
            return resp, resp.json()
        except Exception:
            return resp, None


def _textify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, list):
        return ", ".join(_textify(item) for item in value if _textify(item))
    if isinstance(value, dict):
        for key in ("title", "name", "slug", "ticker", "question", "description"):
            if key in value and value[key]:
                return _textify(value[key])
    return str(value)


def _safe_jsonish(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_jsonish(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _safe_jsonish(val) for key, val in value.items()}
    return str(value)


def _parse_json_response(response: requests.Response) -> Any | None:
    try:
        return response.json()
    except Exception:
        return None


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text:
            return text
    return ""


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(payload, list):
        items.extend(item for item in payload if isinstance(item, dict))
        return items
    if isinstance(payload, dict):
        for key in ("items", "results", "data", "markets", "events", "candlesticks", "candles", "history", "prices_history"):
            value = payload.get(key)
            if isinstance(value, list):
                items.extend(item for item in value if isinstance(item, dict))
        if not items and any(isinstance(v, (str, int, float, bool, list, dict)) for v in payload.values()):
            items.append(payload)
    return items


def _match_keywords(text: str, keywords: Iterable[str]) -> bool:
    lowered = text.lower()
    return all(keyword.lower() in lowered for keyword in keywords)


def _detect_cadence(rows: list[dict[str, Any]]) -> str:
    def _ts(row: dict[str, Any]) -> float | None:
        for key in ("t", "timestamp", "time", "startTs", "start_ts", "ts"):
            value = row.get(key)
            if value in (None, ""):
                continue
            try:
                if isinstance(value, str) and value.endswith("Z"):
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                return float(value)
            except Exception:
                continue
        return None

    timestamps = sorted(ts for ts in (_ts(row) for row in rows) if ts is not None)
    if len(timestamps) < 2:
        return "single-point"
    diffs = [b - a for a, b in zip(timestamps, timestamps[1:]) if b > a]
    if not diffs:
        return "unknown"
    median = sorted(diffs)[len(diffs) // 2]
    if median >= 86400 * 0.9:
        return "daily"
    if median >= 3600 * 0.9:
        return "hourly"
    return "full-resolution"


class PolymarketCollector:
    gamma_base = "https://gamma-api.polymarket.com"
    clob_base = "https://clob.polymarket.com"

    def __init__(self, paths: dict[str, Path], client: HttpClient, writer: RawArtifactWriter, focused: bool = False):
        self.paths = paths
        self.client = client
        self.writer = writer
        self.focused = focused

    def collect(self) -> CollectionResult:
        result = CollectionResult(
            source_id="polymarket",
            source_name="Polymarket",
            source_category="prediction_market",
        )
        discovery_events: list[dict[str, Any]] = []
        candidate_audit_rows: list[dict[str, Any]] = []
        target_queries = [
            ("anthropic_valuation", "will-anthropics-valuation-hit-by-june-30"),
            ("trump_republican_2024", "Trump Republican 2024 presidential election"),
            ("fomc_decision", "FOMC Fed decision"),
        ]
        for target_id, query in target_queries:
            discovery_events.extend(self._resolve_target_event(target_id, query, result, candidate_audit_rows))

        unique_events = self._dedupe_events(discovery_events)
        for event in unique_events:
            records = self._collect_event(event, result)
            result.records.extend(records)

        result.normalized_rows = len(result.records)
        if self.focused:
            debug_path = self.paths["debug"] / "focused_candidate_audit.csv"
            write_csv(
                debug_path,
                [
                    "target_id",
                    "search_query",
                    "candidate_event_id",
                    "slug",
                    "title_question_name",
                    "outcomes",
                    "accepted",
                    "reason",
                    "failed_condition",
                    "source_endpoint",
                ],
                candidate_audit_rows,
            )
        self.writer.write_json(
            "polymarket",
            "audit",
            {
                "attempted_endpoints": result.attempted_endpoints,
                "audit": result.audit,
                "issues": result.issues,
                "normalized_rows": result.normalized_rows,
            },
        )
        return result

    def _resolve_target_event(
        self,
        target_id: str,
        query: str,
        result: CollectionResult,
        candidate_audit_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if target_id == "anthropic_valuation":
            variants = [
                "will-anthropics-valuation-hit-by-june-30",
                "Anthropic valuation June 30",
            ]
        elif target_id == "trump_republican_2024":
            variants = [
                "Trump Republican 2024 presidential election",
                "Trump Republican victory 2024",
                "2024 US presidential election Trump Republican",
            ]
        else:
            variants = [
                "FOMC Fed decision",
                "Federal Reserve rate decision",
                "FOMC meeting decision",
            ]
        resolved: list[dict[str, Any]] = []
        for variant in variants:
            candidates = self._search_events(variant, result)
            resolved.extend(candidates)
            for candidate in candidates:
                accepted, reason, failed = self._evaluate_target_candidate(target_id, candidate)
                endpoint = f"{self.gamma_base}/events?slug={variant}"
                candidate_audit_rows.append(
                    {
                        "target_id": target_id,
                        "search_query": variant,
                        "candidate_event_id": _first_nonempty(candidate.get("id"), candidate.get("event_id")),
                        "slug": _first_nonempty(candidate.get("slug")),
                        "title_question_name": _first_nonempty(candidate.get("title"), candidate.get("question"), candidate.get("name")),
                        "outcomes": " | ".join(self._candidate_outcomes(candidate)),
                        "accepted": "TRUE" if accepted else "FALSE",
                        "reason": reason,
                        "failed_condition": failed,
                        "source_endpoint": endpoint,
                    }
                )
        accepted_events = [event for event in resolved if self._is_exact_target_event(target_id, event)]
        if not accepted_events:
            result.issues.append(f"No clean Polymarket match resolved for {target_id}.")
            return []
        return accepted_events[:1]

    def _candidate_outcomes(self, event: dict[str, Any]) -> list[str]:
        outcomes: list[str] = []
        for key in ("outcomes", "tokens"):
            value = event.get(key)
            if isinstance(value, list):
                outcomes.extend(_textify(item) for item in value if _textify(item))
        markets = event.get("markets")
        if isinstance(markets, list):
            for market in markets:
                if isinstance(market, dict):
                    value = market.get("outcomes")
                    if isinstance(value, list):
                        outcomes.extend(_textify(item) for item in value if _textify(item))
        deduped: list[str] = []
        seen: set[str] = set()
        for outcome in outcomes:
            if outcome not in seen:
                seen.add(outcome)
                deduped.append(outcome)
        return deduped

    def _evaluate_target_candidate(self, target_id: str, event: dict[str, Any]) -> tuple[bool, str, str]:
        text = " ".join(
            _textify(event.get(key))
            for key in ("slug", "title", "question", "name", "description")
            if event.get(key)
        ).lower()
        outcomes = " ".join(self._candidate_outcomes(event)).lower()
        if target_id == "anthropic_valuation":
            variants = ("june 30", "jun 30", "6/30", "by june", "by jun")
            if "anthropic" not in text:
                return False, "Rejected: target text does not mention Anthropic.", "target_text_missing_anthropic"
            if not any(v in text for v in variants):
                return False, "Rejected: June 30 variant not present in text.", "anthropic_date_variant_missing"
            return True, "Accepted: Anthropic text matched and date variant present.", ""
        if target_id == "trump_republican_2024":
            text_ok = ("2024" in text and ("election" in text or "presidential election" in text or "president" in text))
            outcomes_ok = ("trump" in outcomes or "republican" in outcomes)
            if not text_ok:
                return False, "Rejected: event text does not mention a 2024/presidential election.", "election_text_missing"
            if not outcomes_ok:
                return False, "Rejected: outcomes do not mention Trump or Republican.", "outcomes_missing_trump_or_republican"
            return True, "Accepted: 2024 presidential-election text and Trump/Republican outcomes matched.", ""
        text_ok = ("fomc" in text or "fed decision" in text or "federal reserve" in text)
        if not text_ok:
            return False, "Rejected: FOMC/Fed decision text not present.", "fomc_text_missing"
        return True, "Accepted: FOMC/Fed text matched.", ""

    def _is_exact_target_event(self, target_id: str, event: dict[str, Any]) -> bool:
        accepted, _, _ = self._evaluate_target_candidate(target_id, event)
        return accepted

    def _search_events(self, query: str, result: CollectionResult) -> list[dict[str, Any]]:
        candidates = [
            (f"{self.gamma_base}/events", {"slug": query}),
            (f"{self.gamma_base}/events", {"query": query}),
            (f"{self.gamma_base}/public-search", {"query": query}),
            (f"{self.gamma_base}/public-search", {"q": query}),
            (f"{self.gamma_base}/search", {"query": query}),
        ]
        discovered: list[dict[str, Any]] = []
        for url, params in candidates:
            result.attempted_endpoints.append(f"{url}?{params}")
            resp, payload = self.client.get_json(url, params=params)
            result.audit.append({"endpoint": url, "params": params, "status_code": resp.status_code, "query": query})
            raw_path = self.writer.write_text("polymarket", f"search_{_slugify(query)}", resp.text)
            result.raw_files.append(raw_path)
            if resp.ok and payload is not None:
                discovered.extend(self._pull_matching_events(payload, query))
        return discovered

    def _pull_matching_events(self, payload: Any, query: str) -> list[dict[str, Any]]:
        items = _extract_items(payload)
        matches: list[dict[str, Any]] = []
        query_parts = [part for part in re.split(r"\s+", query.replace("-", " ")) if part]
        for item in items:
            text = " ".join(
                _textify(item.get(key))
                for key in ("slug", "title", "question", "name", "description")
                if item.get(key)
            )
            if query.lower() in text.lower() or _match_keywords(text, query_parts[:2]) or "anthropic" in text.lower() or "fomc" in text.lower() or "trump" in text.lower():
                matches.append(item)
        return matches

    def _dedupe_events(self, events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for event in events:
            event_id = _first_nonempty(event.get("id"), event.get("event_id"), event.get("slug"), event.get("question"))
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            unique.append(event)
        return unique

    def _collect_event(self, event: dict[str, Any], result: CollectionResult) -> list[Record]:
        event_id = _first_nonempty(event.get("id"), event.get("event_id"), event.get("slug"), event.get("question"))
        slug = _first_nonempty(event.get("slug"), event_id)
        event_url = _first_nonempty(
            event.get("url"),
            event.get("eventUrl"),
            f"https://polymarket.com/event/{slug}" if slug else "",
        )
        result.audit.append({"event_id": event_id, "slug": slug, "event_url": event_url})
        event_raw_path = self.writer.write_json("polymarket", f"event_{_slugify(slug or event_id)}", _safe_jsonish(event))
        result.raw_files.append(event_raw_path)
        markets = self._extract_markets(event)
        if not markets and event_id:
            markets = self._fetch_markets_for_event(event_id, slug, result)
        result.audit.append({"event_id": event_id, "slug": slug, "markets_found": len(markets)})

        records: list[Record] = []
        for market in markets:
            records.extend(self._collect_market(event, market, event_url, result, event_raw_path))
        return records

    def _extract_markets(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        markets = event.get("markets")
        if isinstance(markets, list):
            return [market for market in markets if isinstance(market, dict)]
        if isinstance(event.get("market"), dict):
            return [event["market"]]
        return []

    def _fetch_markets_for_event(self, event_id: str, slug: str, result: CollectionResult) -> list[dict[str, Any]]:
        candidates = [
            (f"{self.gamma_base}/markets", {"event_id": event_id}),
            (f"{self.gamma_base}/markets", {"eventId": event_id}),
            (f"{self.gamma_base}/markets", {"event_slug": slug}),
            (f"{self.gamma_base}/markets", {"slug": slug}),
        ]
        markets: list[dict[str, Any]] = []
        for url, params in candidates:
            result.attempted_endpoints.append(f"{url}?{params}")
            resp, payload = self.client.get_json(url, params=params)
            result.audit.append({"endpoint": url, "params": params, "status_code": resp.status_code, "event_id": event_id, "slug": slug})
            result.raw_files.append(self.writer.write_text("polymarket", f"markets_{_slugify(event_id)}", resp.text))
            if resp.ok and payload is not None:
                markets.extend(_extract_items(payload))
        return markets

    def _collect_market(
        self,
        event: dict[str, Any],
        market: dict[str, Any],
        event_url: str,
        result: CollectionResult,
        event_raw_path: str,
    ) -> list[Record]:
        market_id = _first_nonempty(market.get("id"), market.get("market_id"), market.get("question"), market.get("slug"))
        market_title = _first_nonempty(market.get("question"), market.get("title"), market.get("name"), market_id)
        condition_id = _first_nonempty(market.get("conditionId"), market.get("condition_id"), market.get("conditionID"))
        token_ids = self._extract_token_ids(market)
        outcomes = self._extract_outcomes(market)
        end_date = _first_nonempty(market.get("endDate"), market.get("end_date"), event.get("endDate"), event.get("end_date"))
        volume = _first_nonempty(market.get("volume"), market.get("volumeNum"), market.get("volume24hr"))
        liquidity = _first_nonempty(market.get("liquidity"), market.get("liquidityNum"), market.get("totalLiquidity"))
        resolution_status = _first_nonempty(market.get("status"), market.get("resolutionStatus"), market.get("resolved"))
        market_url = _first_nonempty(market.get("url"), f"https://polymarket.com/event/{_first_nonempty(event.get('slug'), event.get('id'))}")
        result.audit.append(
            {
                "market_id": market_id,
                "market_title": market_title,
                "condition_id": condition_id,
                "token_count": len(token_ids),
                "outcomes": outcomes,
                "market_url": market_url,
            }
        )
        market_raw_path = self.writer.write_json("polymarket", f"market_{_slugify(market_id)}", _safe_jsonish(market))
        result.raw_files.append(market_raw_path)

        records: list[Record] = []
        price_history_payloads = self._fetch_price_history(token_ids, market_id, result)
        if price_history_payloads:
            for token_index, (token_id, payload, source_endpoint, raw_path) in enumerate(price_history_payloads):
                records.extend(
                    self._history_payload_to_records(
                        event,
                        market,
                        event_url,
                        market_url,
                        condition_id,
                        token_id,
                        outcomes[token_index] if token_index < len(outcomes) else "",
                        end_date,
                        volume,
                        liquidity,
                        resolution_status,
                        payload,
                        source_endpoint,
                        raw_path,
                        event_raw_path,
                    )
                )
        else:
            records.extend(
                self._market_snapshot_to_records(
                    event,
                    market,
                    event_url,
                    market_url,
                    condition_id,
                    token_ids,
                    outcomes,
                    end_date,
                    volume,
                    liquidity,
                    resolution_status,
                    market_raw_path,
                )
            )
        return records

    def _extract_token_ids(self, market: dict[str, Any]) -> list[str]:
        values = []
        for key in ("clobTokenIds", "clob_token_ids", "tokenIds", "token_ids"):
            value = market.get(key)
            if isinstance(value, list):
                values.extend(_first_nonempty(item) for item in value)
            elif isinstance(value, str) and value:
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        values.extend(_first_nonempty(item) for item in parsed)
                except Exception:
                    values.append(value)
        return [value for value in values if value]

    def _extract_outcomes(self, market: dict[str, Any]) -> list[str]:
        for key in ("outcomes", "tokens", "clobTokenIds"):
            value = market.get(key)
            if isinstance(value, list):
                outcomes = [_textify(item) for item in value if _textify(item)]
                if outcomes:
                    return outcomes
        return []

    def _fetch_price_history(
        self,
        token_ids: list[str],
        market_id: str,
        result: CollectionResult,
    ) -> list[tuple[str, Any, str, str]]:
        payloads: list[tuple[str, Any, str, str]] = []
        for token_id in token_ids:
            for params in (
                {"market": token_id, "interval": "1d"},
                {"market": token_id, "interval": "1h"},
                {"market": token_id, "interval": "6h"},
            ):
                url = f"{self.clob_base}/prices-history"
                result.attempted_endpoints.append(f"{url}?{params}")
                resp, payload = self.client.get_json(url, params=params)
                result.audit.append({"endpoint": url, "params": params, "status_code": resp.status_code, "token_id": token_id, "market_id": market_id})
                raw_path = self.writer.write_text("polymarket", f"prices_history_{_slugify(token_id)}", resp.text)
                result.raw_files.append(raw_path)
                items = _extract_items(payload)
                if resp.ok and payload is not None and items:
                    payloads.append((token_id, payload, f"{url}?{params}", raw_path))
                    result.audit.append(
                        {
                            "token_id": token_id,
                            "history_points": len(items),
                            "cadence": _detect_cadence(items),
                            "source_endpoint": f"{url}?{params}",
                        }
                    )
                    break
        if not payloads:
            result.issues.append(f"Polymarket price history returned no usable rows for market {market_id}.")
        return payloads

    def _history_payload_to_records(
        self,
        event: dict[str, Any],
        market: dict[str, Any],
        event_url: str,
        market_url: str,
        condition_id: str,
        token_id: str,
        outcome_label: str,
        end_date: str,
        volume: str,
        liquidity: str,
        resolution_status: str,
        payload: Any,
        source_endpoint: str,
        raw_path: str,
        event_raw_path: str,
    ) -> list[Record]:
        rows = _extract_items(payload)
        records: list[Record] = []
        for idx, row in enumerate(rows, start=1):
            price = self._extract_price(row)
            ts = self._extract_timestamp(row)
            if not price or ts is None:
                continue
            observation_date = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
            record = Record(
                source_id="polymarket",
                source_name="Polymarket",
                source_category="prediction_market",
                source_group="prediction_market",
                platform="Polymarket",
                series_kind="probability",
                benchmark_type="market_probability",
                event_id=_first_nonempty(event.get("id"), event.get("event_id"), event.get("slug")),
                event_name=_first_nonempty(event.get("title"), event.get("name"), event.get("slug")),
                category=_first_nonempty(market.get("category"), event.get("category"), "prediction_market"),
                observation_date=observation_date,
                event_date=_first_nonempty(end_date),
                raw_value=str(price),
                probability_value=str(price),
                benchmark_value="",
                unit="probability",
                use_in_probability_analysis="TRUE" if price is not None else "FALSE",
                data_status="real_api_data",
                is_real_data="TRUE",
                source_endpoint=source_endpoint,
                source_url=market_url or event_url,
                raw_file_path=raw_path,
                provenance_file=event_raw_path,
                provenance_row=str(idx),
                notes="; ".join(
                    part
                    for part in [
                        f"token_id={token_id}",
                        f"condition_id={condition_id}" if condition_id else "",
                        f"outcome={outcome_label}" if outcome_label else "",
                        f"volume={volume}" if volume else "",
                        f"liquidity={liquidity}" if liquidity else "",
                        f"resolution_status={resolution_status}" if resolution_status else "",
                    ]
                    if part
                ),
            )
            records.append(record)
        return records

    def _market_snapshot_to_records(
        self,
        event: dict[str, Any],
        market: dict[str, Any],
        event_url: str,
        market_url: str,
        condition_id: str,
        token_ids: list[str],
        outcomes: list[str],
        end_date: str,
        volume: str,
        liquidity: str,
        resolution_status: str,
        raw_path: str,
    ) -> list[Record]:
        records: list[Record] = []
        prices = self._extract_prices(market)
        for idx, price in enumerate(prices or [""]):
            outcome_label = outcomes[idx] if idx < len(outcomes) else ""
            record = Record(
                source_id="polymarket",
                source_name="Polymarket",
                source_category="prediction_market",
                source_group="prediction_market",
                platform="Polymarket",
                series_kind="probability",
                benchmark_type="market_probability",
                event_id=_first_nonempty(event.get("id"), event.get("event_id"), event.get("slug")),
                event_name=_first_nonempty(event.get("title"), event.get("name"), event.get("slug")),
                category=_first_nonempty(market.get("category"), event.get("category"), "prediction_market"),
                observation_date=datetime.now(timezone.utc).date().isoformat(),
                event_date=_first_nonempty(end_date),
                raw_value=str(price),
                probability_value=str(price),
                benchmark_value="",
                unit="probability",
                use_in_probability_analysis="TRUE" if price not in ("", None) else "FALSE",
                data_status="real_api_data",
                is_real_data="TRUE",
                source_endpoint=f"{self.gamma_base}/events snapshot",
                source_url=market_url or event_url,
                raw_file_path=raw_path,
                provenance_file=raw_path,
                provenance_row=str(idx + 1),
                notes="; ".join(
                    part
                    for part in [
                        f"condition_id={condition_id}" if condition_id else "",
                        f"token_id={token_ids[idx]}" if idx < len(token_ids) else "",
                        f"outcome={outcome_label}" if outcome_label else "",
                        f"volume={volume}" if volume else "",
                        f"liquidity={liquidity}" if liquidity else "",
                        f"resolution_status={resolution_status}" if resolution_status else "",
                    ]
                    if part
                ),
            )
            records.append(record)
        return records

    def _extract_prices(self, market: dict[str, Any]) -> list[str]:
        for key in ("outcomePrices", "prices", "lastTradePrices"):
            value = market.get(key)
            if isinstance(value, list):
                return [str(item) for item in value]
            if isinstance(value, str) and value:
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, list):
                        return [str(item) for item in parsed]
                except Exception:
                    return [value]
        return []

    def _extract_price(self, row: dict[str, Any]) -> str:
        for key in ("p", "price", "value", "close", "close_price", "mean"):
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
        if "prices" in row and isinstance(row["prices"], dict):
            for key in ("p", "price", "value"):
                value = row["prices"].get(key)
                if value not in (None, ""):
                    return str(value)
        return ""

    def _extract_timestamp(self, row: dict[str, Any]) -> float | None:
        for key in ("t", "timestamp", "time", "start_ts", "ts"):
            value = row.get(key)
            if value in (None, ""):
                continue
            try:
                return float(value)
            except Exception:
                continue
        return None


class KalshiCollector:
    base_url = "https://api.elections.kalshi.com/trade-api/v2"

    def __init__(self, paths: dict[str, Path], client: HttpClient, writer: RawArtifactWriter, focused: bool = False):
        self.paths = paths
        self.client = client
        self.writer = writer
        self.focused = focused

    def collect(self) -> CollectionResult:
        result = CollectionResult(
            source_id="kalshi",
            source_name="Kalshi",
            source_category="prediction_market",
        )
        markets = self._discover_markets(result)
        selected = self._filter_markets(markets)
        if not selected:
            result.issues.append("No Kalshi markets matched the exact Trump/FOMC target filters.")
        for market in selected:
            result.records.extend(self._collect_market(market, result))
        if not result.records and not result.issues:
            result.issues.append("Kalshi endpoints were reachable, but no candlestick rows normalized.")
        result.normalized_rows = len(result.records)
        return result

    def _discover_markets(self, result: CollectionResult) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        queries = [
            ("trump_victory_2024", "Trump Republican 2024 presidential election"),
            ("fomc_decision", "FOMC Fed decision"),
        ]
        for label, query in queries:
            url = f"{self.base_url}/events"
            params = {"limit": 25, "search": query, "with_nested_markets": "true"}
            result.attempted_endpoints.append(f"{url}?{params}")
            resp, payload = self.client.get_json(url, params=params)
            raw_path = self.writer.write_text("kalshi", f"events_{label}", resp.text)
            result.raw_files.append(raw_path)
            result.audit.append({"endpoint": url, "params": params, "status_code": resp.status_code, "query": query})
            if not resp.ok or payload is None:
                continue
            items = _extract_items(payload)
            collected.extend(items)
        # Deduplicate by ticker or title before filtering further.
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in collected:
            key = _first_nonempty(item.get("ticker"), item.get("event_ticker"), item.get("title"), item.get("question"))
            if key and key not in seen:
                seen.add(key)
                unique.append(item)
        collected = unique[:15]
        return collected

    def _filter_markets(self, markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for event in markets:
            text = " ".join(
                _textify(event.get(key))
                for key in ("ticker", "title", "subtitle", "event_ticker", "event_title", "category")
                if event.get(key)
            ).lower()
            if "anthropic" in text:
                continue
            if "fomc" in text or ("fed" in text and "decision" in text):
                ticker = _first_nonempty(event.get("ticker"), event.get("market_ticker"), event.get("event_ticker"))
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    selected.append(event)
                continue
            if "trump" in text and "2024" in text and ("president" in text or "electoral" in text or "republican" in text):
                ticker = _first_nonempty(event.get("ticker"), event.get("market_ticker"), event.get("event_ticker"))
                if ticker and ticker not in seen:
                    seen.add(ticker)
                    selected.append(event)
        return selected

    def _collect_market(self, market: dict[str, Any], result: CollectionResult) -> list[Record]:
        ticker = _first_nonempty(market.get("ticker"), market.get("market_ticker"), market.get("event_ticker"))
        series_ticker = _first_nonempty(market.get("event_ticker"), market.get("series_ticker"))
        if not ticker or not series_ticker:
            return []
        market_detail = self._fetch_market_detail(ticker, result)
        candle_rows = self._fetch_candles(series_ticker, ticker, result)
        raw_path = self.writer.write_json("kalshi", f"market_{_slugify(ticker)}", _safe_jsonish(market_detail or market))
        result.raw_files.append(raw_path)
        records: list[Record] = []
        source = market_detail or market
        print(
            "Kalshi candidate:",
            {
                "ticker": ticker,
                "title": _first_nonempty(source.get("title"), source.get("subtitle"), ticker),
                "category": _first_nonempty(source.get("category"), source.get("sector")),
                "close_time": _first_nonempty(source.get("close_time"), source.get("close_ts"), source.get("end_ts")),
                "status": _first_nonempty(source.get("status")),
                "volume": _first_nonempty(source.get("volume_fp"), source.get("volume_24h_fp"), source.get("volume_dollars")),
                "open_interest": _first_nonempty(source.get("open_interest_fp"), source.get("open_interest")),
            },
        )
        for idx, row in enumerate(candle_rows, start=1):
            price = self._extract_price(row)
            ts = self._extract_timestamp(row)
            if price in ("", None) or ts is None:
                continue
            records.append(
                Record(
                    source_id="kalshi",
                    source_name="Kalshi",
                    source_category="prediction_market",
                    source_group="prediction_market",
                    platform="Kalshi",
                    series_kind="probability",
                    benchmark_type="market_probability",
                    event_id=ticker,
                    event_name=_first_nonempty(source.get("title"), source.get("subtitle"), source.get("title_line"), ticker),
                    category=_first_nonempty(source.get("category"), source.get("sector"), "prediction_market"),
                    observation_date=datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat(),
                    event_date=_first_nonempty(source.get("close_ts"), source.get("end_ts"), source.get("expected_close_ts")),
                    raw_value=str(price),
                    probability_value=str(price),
                    benchmark_value="",
                    unit="probability",
                    use_in_probability_analysis="TRUE",
                    data_status="real_api_data",
                    is_real_data="TRUE",
                    source_endpoint=f"{self.base_url}/markets/{ticker}/candlesticks",
                    source_url=_first_nonempty(source.get("url"), f"https://kalshi.com/markets/{ticker}"),
                    raw_file_path=raw_path,
                    provenance_file=raw_path,
                    provenance_row=str(idx),
                    notes="; ".join(
                        part
                        for part in [
                            f"ticker={ticker}",
                            f"status={_first_nonempty(source.get('status'))}",
                        ]
                        if part
                    ),
                )
            )
        if not records:
            result.issues.append(f"Kalshi exact market matched but no candlestick rows were returned for {ticker}.")
        return records

    def _fetch_market_detail(self, ticker: str, result: CollectionResult) -> dict[str, Any] | None:
        candidates = [
            (f"{self.base_url}/markets/{ticker}", {}),
            (f"{self.base_url}/markets", {"ticker": ticker}),
        ]
        for url, params in candidates:
            result.attempted_endpoints.append(f"{url}?{params}")
            resp, payload = self.client.get_json(url, params=params)
            result.raw_files.append(self.writer.write_text("kalshi", f"detail_{_slugify(ticker)}", resp.text))
            result.audit.append({"endpoint": url, "params": params, "status_code": resp.status_code, "ticker": ticker})
            if resp.ok and isinstance(payload, dict):
                return payload.get("market") if isinstance(payload.get("market"), dict) else payload
        return None

    def _fetch_candles(self, series_ticker: str, ticker: str, result: CollectionResult) -> list[dict[str, Any]]:
        candidates = [
            (f"{self.base_url}/series/{series_ticker}/markets/{ticker}/candlesticks", {"period_interval": 1440}),
            (f"{self.base_url}/historical/series/{series_ticker}/markets/{ticker}/candlesticks", {"period_interval": 1440}),
            (f"{self.base_url}/series/{series_ticker}/markets/{ticker}/candlesticks", {"period_interval": "1d"}),
            (f"{self.base_url}/historical/series/{series_ticker}/markets/{ticker}/candlesticks", {"period_interval": "1d"}),
        ]
        rows: list[dict[str, Any]] = []
        start_ts = int(datetime.now(timezone.utc).timestamp()) - 86400 * 370
        end_ts = int(datetime.now(timezone.utc).timestamp())
        for url, params in candidates:
            params = {**params, "startTs": start_ts, "endTs": end_ts}
            result.attempted_endpoints.append(f"{url}?{params}")
            resp, payload = self.client.get_json(url, params=params)
            result.raw_files.append(self.writer.write_text("kalshi", f"candles_{_slugify(series_ticker)}_{_slugify(ticker)}", resp.text))
            result.audit.append({"endpoint": url, "params": params, "status_code": resp.status_code, "series_ticker": series_ticker, "ticker": ticker})
            if resp.ok and payload is not None:
                rows.extend(_extract_items(payload))
                if rows:
                    break
        if not rows:
            result.issues.append(f"No Kalshi candlestick rows normalized for {ticker} / {series_ticker}.")
        return rows

    def _extract_price(self, row: dict[str, Any]) -> str:
        for key in ("close_dollars", "close", "close_price", "price", "p", "mean", "average", "last_price_dollars", "yes_ask_dollars", "yes_bid_dollars"):
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    def _extract_timestamp(self, row: dict[str, Any]) -> float | None:
        for key in ("ts", "timestamp", "time", "start_ts", "end_ts", "close_time"):
            value = row.get(key)
            if value in (None, ""):
                continue
            try:
                if isinstance(value, str) and value.endswith("Z"):
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                return float(value)
            except Exception:
                continue
        return None


class FiveThirtyEightCollector:
    github_api = "https://api.github.com"
    raw_base = "https://raw.githubusercontent.com"
    repo = "fivethirtyeight/data"

    def __init__(self, paths: dict[str, Path], client: HttpClient, writer: RawArtifactWriter, focused: bool = False):
        self.paths = paths
        self.client = client
        self.writer = writer
        self.focused = focused

    def collect(self) -> CollectionResult:
        result = CollectionResult(
            source_id="election_benchmarks",
            source_name="FiveThirtyEight",
            source_category="professional_benchmark",
        )
        datasets = self._discover_datasets(result)
        for dataset in datasets:
            result.records.extend(self._collect_dataset(dataset, result))
        result.normalized_rows = len(result.records)
        return result

    def _discover_datasets(self, result: CollectionResult) -> list[dict[str, Any]]:
        datasets: list[dict[str, Any]] = []
        urls = [
            "https://projects.fivethirtyeight.com/polls/data/presidential_general_averages.csv",
        ]
        for url in urls:
            result.attempted_endpoints.append(url)
            resp = self.client.get(url)
            result.raw_files.append(self.writer.write_text("fivethirtyeight", f"download_{_slugify(url)}", resp.text))
            if resp.ok:
                datasets.append({"name": url.rsplit("/", 1)[-1], "download_url": url, "type": "file"})
        datasets = self._dedupe(datasets)
        if not datasets:
            result.issues.append("No downloadable FiveThirtyEight polling CSVs were discovered from the public GitHub tree or README.")
        return datasets

    def _datasets_from_readme(self, text: str) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for match in re.finditer(r"https://[^)\s]+?\.csv", text):
            url = match.group(0)
            name = url.rsplit("/", 1)[-1]
            if any(term in name.lower() for term in ("poll", "presidential", "generic", "election", "forecast")):
                candidates.append({"name": name, "download_url": url, "type": "file"})
        return candidates

    def _dedupe(self, datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for item in datasets:
            key = _first_nonempty(item.get("download_url"), item.get("name"))
            if key and key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def _collect_dataset(self, dataset: dict[str, Any], result: CollectionResult) -> list[Record]:
        url = _first_nonempty(dataset.get("download_url"), dataset.get("html_url"))
        name = _first_nonempty(dataset.get("name"), url.rsplit("/", 1)[-1] if url else "")
        if not url:
            return []
        result.attempted_endpoints.append(url)
        resp = self.client.get(url)
        raw_path = self.writer.write_text("fivethirtyeight", f"dataset_{_slugify(name)}", resp.text)
        result.raw_files.append(raw_path)
        if not resp.ok:
            return []
        rows = self._parse_csv(resp.text)
        out: list[Record] = []
        for idx, row in enumerate(rows, start=1):
            if not self._row_is_relevant_2024_presidential(row):
                continue
            benchmark_value = self._extract_benchmark_value(row)
            if benchmark_value == "":
                continue
            observation_date = _first_nonempty(
                row.get("modeldate"),
                row.get("date"),
                row.get("start_date"),
                row.get("end_date"),
                row.get("timestamp"),
            )
            out.append(
                Record(
                    source_id="election_benchmarks",
                    source_name="FiveThirtyEight",
                    source_category="professional_benchmark",
                    source_group="benchmark",
                    platform="FiveThirtyEight",
                    series_kind="benchmark",
                    benchmark_type="polling_average" if "average" in name.lower() or "average" in row.get("notes", "").lower() else "poll_data",
                    event_id=_first_nonempty(row.get("race_id"), row.get("poll_id"), name),
                    event_name=_first_nonempty(row.get("race"), row.get("pollster"), row.get("question"), name),
                    category=_first_nonempty(row.get("subgroup"), row.get("type"), "benchmark"),
                    observation_date=observation_date,
                    event_date=_first_nonempty(row.get("end_date"), row.get("date")),
                    raw_value=benchmark_value,
                    probability_value="",
                    benchmark_value=benchmark_value,
                    unit=_first_nonempty(row.get("unit"), "benchmark"),
                    use_in_probability_analysis="FALSE",
                    data_status="real_downloaded_data",
                    is_real_data="TRUE",
                    source_endpoint=url,
                    source_url=url,
                    raw_file_path=raw_path,
                    provenance_file=raw_path,
                    provenance_row=str(idx),
                    notes=f"FiveThirtyEight dataset {name}.",
                )
            )
        if not out:
            result.issues.append(f"No 2024 presidential benchmark rows normalized from {name}.")
        return out

    def _parse_csv(self, text: str) -> list[dict[str, str]]:
        from io import StringIO

        reader = csv.DictReader(StringIO(text))
        return [dict(row) for row in reader]

    def _extract_benchmark_value(self, row: dict[str, str]) -> str:
        for key in ("pct_estimate", "pct_trend_adjusted", "pct", "polling_average", "average", "value", "forecast", "dem", "rep", "margin"):
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    def _row_is_relevant_2024_presidential(self, row: dict[str, str]) -> bool:
        cycle = _first_nonempty(row.get("cycle"), row.get("modeldate"), row.get("date")).lower()
        if "2024" not in cycle:
            return False
        text = " ".join(_first_nonempty(row.get(key)) for key in ("candidate_name", "party", "question")).lower()
        state = _first_nonempty(row.get("state"), row.get("subgroup"), row.get("location")).lower()
        if state and state not in {"national", "us", "u.s.", "usa"}:
            return False
        return any(term in text for term in ("trump", "kamala harris", "biden", "republican", "democrat"))


class CmeFedwatchCollector:
    def __init__(self, paths: dict[str, Path], client: HttpClient, writer: RawArtifactWriter):
        self.paths = paths
        self.client = client
        self.writer = writer

    def collect(self) -> CollectionResult:
        result = CollectionResult(
            source_id="cme_fedwatch",
            source_name="CME FedWatch",
            source_category="professional_benchmark",
        )
        # Best-effort public access only. Try the tool page and document whether data is actually exposed.
        urls = [
            "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
            "https://www.cmegroup.com/CmeWS/mvc/FedWatchTool/ToolData",
        ]
        for url in urls:
            result.attempted_endpoints.append(url)
            resp = self.client.get(url)
            result.raw_files.append(self.writer.write_text("cme_fedwatch", f"probe_{_slugify(url)}", resp.text))
            if resp.ok and "application/json" in resp.headers.get("Content-Type", ""):
                try:
                    payload = resp.json()
                except Exception:
                    continue
                rows = self._payload_to_records(payload, url, result)
                if rows:
                    result.records.extend(rows)
                    result.normalized_rows = len(result.records)
                    return result
        result.issues.append(
            "CME FedWatch public probabilities were not accessible from the documented public page/API. The tool likely requires a licensed or credentialed data service."
        )
        return result

    def _payload_to_records(self, payload: Any, url: str, result: CollectionResult) -> list[Record]:
        rows: list[Record] = []
        items = _extract_items(payload)
        for idx, item in enumerate(items, start=1):
            probability = _first_nonempty(item.get("probability"), item.get("value"), item.get("pct"))
            if not probability:
                continue
            rows.append(
                Record(
                    source_id="cme_fedwatch",
                    source_name="CME FedWatch",
                    source_category="professional_benchmark",
                    source_group="benchmark",
                    platform="CME FedWatch",
                    series_kind="probability",
                    benchmark_type="futures_implied_probability",
                    event_id=_first_nonempty(item.get("meeting"), item.get("meeting_date"), "fedwatch"),
                    event_name=_first_nonempty(item.get("meeting"), item.get("meeting_date"), "FedWatch"),
                    category="benchmark",
                    observation_date=datetime.now(timezone.utc).date().isoformat(),
                    event_date=_first_nonempty(item.get("meeting_date"), item.get("meeting")),
                    raw_value=probability,
                    probability_value=probability,
                    benchmark_value="",
                    unit="probability",
                    use_in_probability_analysis="TRUE",
                    data_status="partial",
                    is_real_data="TRUE",
                    source_endpoint=url,
                    source_url=url,
                    raw_file_path="",
                    provenance_file=url,
                    provenance_row=str(idx),
                    notes="CME FedWatch accessible via public page/API.",
                )
            )
        return rows


class AnthropicCollector:
    def __init__(self, paths: dict[str, Path], client: HttpClient, writer: RawArtifactWriter):
        self.paths = paths
        self.client = client
        self.writer = writer

    def collect(self) -> CollectionResult:
        result = CollectionResult(
            source_id="anthropic_references",
            source_name="Anthropic References",
            source_category="manual_annotation",
        )
        urls = [
            "https://www.anthropic.com/news",
            "https://www.anthropic.com/newsroom",
        ]
        for url in urls:
            result.attempted_endpoints.append(url)
            resp = self.client.get(url)
            raw_path = self.writer.write_text("anthropic_references", f"page_{_slugify(url)}", resp.text)
            result.raw_files.append(raw_path)
            if resp.ok:
                result.records.extend(self._extract_annotations(resp.text, url, raw_path))
        result.normalized_rows = len(result.records)
        return result

    def _extract_annotations(self, html: str, url: str, raw_path: str) -> list[Record]:
        rows: list[Record] = []
        seen: set[str] = set()
        for href, title in re.findall(r'href="([^"]+)"[^>]*>([^<]{3,180})<', html, flags=re.IGNORECASE):
            combined = f"{href} {title}".lower()
            if not any(keyword in combined for keyword in ("valuation", "funding", "raised", "news", "anthropic")):
                continue
            if href in seen:
                continue
            seen.add(href)
            full_url = href if href.startswith("http") else f"https://www.anthropic.com{href}"
            rows.append(
                Record(
                    source_id="anthropic_references",
                    source_name="Anthropic References",
                    source_category="manual_annotation",
                    source_group="qualitative",
                    platform="Anthropic",
                    series_kind="qualitative",
                    benchmark_type="qualitative_news",
                    event_id=_slugify(title) or _slugify(href),
                    event_name=title.strip(),
                    category="anthropic",
                    observation_date=datetime.now(timezone.utc).date().isoformat(),
                    event_date="",
                    raw_value=title.strip(),
                    probability_value="",
                    benchmark_value=title.strip(),
                    unit="text",
                    use_in_probability_analysis="FALSE",
                    data_status="manual_annotation",
                    is_real_data="TRUE",
                    source_endpoint=url,
                    source_url=full_url,
                    raw_file_path=raw_path,
                    provenance_file=raw_path,
                    provenance_row="1",
                    notes="Public Anthropic news or newsroom reference.",
                )
            )
        return rows
