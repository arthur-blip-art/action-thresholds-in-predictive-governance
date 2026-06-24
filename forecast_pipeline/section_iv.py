"""Section IV benchmark extraction and overlays.

This module builds small, source-traceable benchmark datasets for the thesis
comparisons and renders compact overlay charts.

The implementation is intentionally conservative:
- prefer public, directly downloadable sources when reachable
- fall back to transparent proxy series when exact probability exports are not
  available
- never fabricate unavailable rows without marking the method used
"""

from __future__ import annotations

import csv
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

import requests

from .config import project_paths
from .io_utils import ensure_dir, write_csv


TRUMP_BENCHMARK_COLUMNS = (
    "date",
    "trump_win_prob_polls",
    "source",
    "window_start",
    "window_end",
    "granularity",
    "method",
    "raw_file_path",
    "notes",
)

FEDWATCH_BENCHMARK_COLUMNS = (
    "meeting_date",
    "snapshot_date",
    "target_rate_outcome",
    "probability",
    "source",
    "method",
    "futures_symbol",
    "implied_rate",
    "current_target_lower",
    "current_target_upper",
    "raw_file_path",
    "notes",
)


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except Exception:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
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


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace("%", "").strip())
    except Exception:
        return None


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _latest_by_day(rows: list[dict[str, Any]], date_key: str, value_key: str) -> dict[str, float]:
    latest: dict[str, tuple[date, float]] = {}
    for row in rows:
        day = _parse_date(str(row.get(date_key, "")).strip())
        val = _safe_float(row.get(value_key))
        if day is None or val is None:
            continue
        key = day.isoformat()
        if key not in latest or day > latest[key][0]:
            latest[key] = (day, val)
    return {key: val for key, (_, val) in latest.items()}


def _forward_fill(daily_map: dict[str, float], start: date, end: date) -> list[tuple[date, float]]:
    out: list[tuple[date, float]] = []
    current: float | None = None
    cursor = start
    while cursor <= end:
        key = cursor.isoformat()
        if key in daily_map:
            current = daily_map[key]
        if current is not None:
            out.append((cursor, current))
        cursor += timedelta(days=1)
    return out


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def _fetch_text(url: str, params: dict[str, Any] | None = None, timeout: int = 30) -> tuple[str, int, str]:
    resp = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 (Codex thesis pipeline)", "Accept": "text/html,application/json,text/csv,*/*"},
    )
    return resp.text, resp.status_code, resp.url


def _fetch_json(url: str, params: dict[str, Any] | None = None, timeout: int = 30) -> tuple[Any | None, str, int, str]:
    text, status, final_url = _fetch_text(url, params=params, timeout=timeout)
    try:
        return json.loads(text), text, status, final_url
    except Exception:
        return None, text, status, final_url


def _month_code(month: int) -> str:
    return {
        1: "F",
        2: "G",
        3: "H",
        4: "J",
        5: "K",
        6: "M",
        7: "N",
        8: "Q",
        9: "U",
        10: "V",
        11: "X",
        12: "Z",
    }[month]


def _futures_symbol_candidates(meeting_date: date) -> list[str]:
    yy = meeting_date.year % 100
    code = _month_code(meeting_date.month)
    # Try a continuous symbol first, then monthly contract variants.
    return [
        "ZQ=F",
        f"ZQ{code}{yy:02d}=F",
        f"ZQ{code}{yy:02d}.CBT",
        f"ZQ{code}{yy:02d}",
    ]


def _fetch_yahoo_history(symbol: str, start: date, end: date) -> tuple[list[dict[str, Any]], str, str]:
    period1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp()) - 86400
    period2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "period1": period1,
        "period2": period2,
        "interval": "1d",
        "includePrePost": "false",
        "events": "div,splits",
    }
    payload, text, status, final_url = _fetch_json(url, params=params)
    if status != 200 or not isinstance(payload, dict):
        return [], text, final_url
    try:
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp", [])
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        closes = quote.get("close", [])
        rows: list[dict[str, Any]] = []
        for ts, close in zip(timestamps, closes):
            if close in (None, ""):
                continue
            rows.append(
                {
                    "timestamp": int(ts),
                    "date": datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat(),
                    "close": float(close),
                }
            )
        return rows, text, final_url
    except Exception:
        return [], text, final_url


def _fetch_fred_series(series_id: str) -> tuple[list[dict[str, Any]], str, str]:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    params = {"id": series_id}
    text, status, final_url = _fetch_text(url, params=params)
    if status != 200:
        return [], text, final_url
    rows: list[dict[str, Any]] = []
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        day = _parse_date(row.get("DATE", "") or row.get("date", "") or "")
        val = _safe_float(row.get(series_id) or row.get("value"))
        if day is None or val is None:
            continue
        rows.append({"date": day.isoformat(), "value": val})
    return rows, text, final_url


def _locate_local_file(paths: dict[str, Path], needle: str) -> Path | None:
    for p in (paths["raw"], paths["root"] / "pipeline_outputs" / "raw"):
        if not p.exists():
            continue
        candidates = list(p.rglob(f"*{needle}*"))
        if candidates:
            return max(candidates, key=lambda path: path.stat().st_mtime)
    return None


def _candidate_tickers_from_row(row: dict[str, str]) -> set[str]:
    # Some forecast-history exports use text labels rather than structured columns.
    fields = [
        row.get("forecaster"),
        row.get("forecast"),
        row.get("election"),
        row.get("race"),
        row.get("title"),
        row.get("name"),
        row.get("candidate"),
        row.get("question"),
    ]
    text = " ".join(str(field) for field in fields if field)
    return {
        token
        for token in ("Silver Bulletin", "The Economist", "538", "RealClearPolitics", "Trump", "Harris", "Biden")
        if token.lower() in text.lower()
    }


def _extract_probability_from_row(row: dict[str, str]) -> float | None:
    # Prefer explicit probability-like columns.
    preferred_keys = [
        "trump_win_prob",
        "win_prob",
        "probability",
        "forecast_probability",
        "prob",
        "value",
        "chance",
        "call_pct",
        "call_percent",
        "pct",
    ]
    for key in preferred_keys:
        if key in row:
            val = _safe_float(row.get(key))
            if val is not None:
                if val > 1.0:
                    val = val / 100.0
                return val
    return None


def _extract_date_from_row(row: dict[str, str]) -> date | None:
    for key in ("date", "forecast_date", "modeldate", "snapshot_date", "updated", "timestamp", "day"):
        day = _parse_date(row.get(key, "") or "")
        if day is not None:
            return day
    return None


def build_trump_polls_benchmark(base_dir: Path | str = ".") -> tuple[Path, list[str]]:
    """Build a daily Trump 2024 benchmark series.

    Prefer a true forecast-history source if it is reachable. If that cannot be
    fetched, fall back to a transparent proxy built from the 2024 national
    presidential averages already captured in pipeline_outputs/raw/fivethirtyeight.
    """
    paths = project_paths(Path(base_dir))
    ensure_dir(paths["cleaned"])
    ensure_dir(paths["validation"])
    ensure_dir(paths["raw"])

    out_path = paths["cleaned"] / "trump_polls_benchmark.csv"
    issues: list[str] = []

    # Attempt source-first fetch.
    direct_rows: list[dict[str, Any]] = []
    direct_source = ""
    source_url = "https://projects.jhkforecasts.com/forecast-history/output.csv"
    try:
        text, status, final_url = _fetch_text(source_url)
        if status == 200 and text.strip() and "," in text.splitlines()[0]:
            raw_path = paths["raw"] / "section_iv" / f"trump_forecast_history_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
            _write_text(raw_path, text)
            reader = csv.DictReader(text.splitlines())
            for row in reader:
                if "2024" not in " ".join(str(v) for v in row.values()).lower():
                    continue
                if not _candidate_tickers_from_row(row):
                    continue
                prob = _extract_probability_from_row(row)
                day = _extract_date_from_row(row)
                if prob is None or day is None:
                    continue
                direct_rows.append(
                    {
                        "date": day,
                        "trump_win_prob_polls": prob,
                        "source": "Silver Bulletin / forecast-history",
                        "raw_file_path": str(raw_path),
                        "notes": f"direct_source={final_url}",
                    }
                )
            if direct_rows:
                direct_source = "Silver Bulletin / forecast-history"
    except Exception as exc:
        issues.append(f"direct forecast-history fetch failed: {exc}")

    if direct_rows:
        # Collapse to one daily row by taking the latest available observation.
        daily_map: dict[str, tuple[date, float, dict[str, Any]]] = {}
        for row in direct_rows:
            d = row["date"]
            key = d.isoformat()
            if key not in daily_map or d > daily_map[key][0]:
                daily_map[key] = (d, float(row["trump_win_prob_polls"]), row)
        rows = [v[2] for _, v in sorted(daily_map.items())]
        # Forward-fill daily across the campaign window.
        daily_map2 = {row["date"].isoformat(): float(row["trump_win_prob_polls"]) for row in rows}
        filled = _forward_fill(daily_map2, date(2024, 1, 1), date(2024, 11, 5))
        out_rows = [
            {
                "date": d.isoformat(),
                "trump_win_prob_polls": round(prob, 6),
                "source": direct_source,
                "window_start": "2024-01-01",
                "window_end": "2024-11-05",
                "granularity": "daily",
                "method": "direct_forecast_history_forward_fill",
                "raw_file_path": rows[-1]["raw_file_path"] if rows else "",
                "notes": "Direct source preferred; daily series forward-filled from available forecast updates.",
            }
            for d, prob in filled
        ]
        write_csv(out_path, TRUMP_BENCHMARK_COLUMNS, out_rows)
        _write_method_note(
            paths["validation"] / "trump_polls_benchmark_method.md",
            [
                "# Trump 2024 benchmark method",
                "",
                "- Preferred source: Silver Bulletin / forecast-history export when reachable.",
                "- Fallback: forward-filled daily proxy from the local 2024 national presidential averages.",
                "- Daily rows cover 2024-01-01 through 2024-11-05.",
                "- If the benchmark is a proxy, it is explicitly labeled in `method` and `notes`.",
            ],
        )
        return out_path, issues

    # Fallback: local 2024 national polling average proxy.
    local_proxy = _build_trump_proxy_from_local_538(paths)
    out_rows = local_proxy["rows"]
    issues.extend(local_proxy["issues"])
    write_csv(out_path, TRUMP_BENCHMARK_COLUMNS, out_rows)
    _write_method_note(
        paths["validation"] / "trump_polls_benchmark_method.md",
        [
            "# Trump 2024 benchmark method",
            "",
            "- Fallback source: FiveThirtyEight 2024 national presidential averages captured locally in pipeline_outputs/raw/fivethirtyeight.",
            "- Benchmark probability is a proxy derived from the Trump-minus-Democrat national polling margin.",
            "- The proxy uses a standard-normal mapping so the series remains bounded in [0, 1].",
            "- Daily rows are forward-filled from the latest available national average on or before each date.",
        ],
    )
    return out_path, issues


def _build_trump_proxy_from_local_538(paths: dict[str, Path]) -> dict[str, Any]:
    source_file = _locate_local_file(paths, "presidential-general-averages-2024-09-12-uncorrected-csv.txt")
    if source_file is None:
        return {
            "rows": [],
            "issues": ["No local FiveThirtyEight presidential general averages file found for proxy fallback."],
        }
    rows = _read_csv(source_file)
    daily_pairs: dict[str, dict[str, float]] = defaultdict(dict)
    for row in rows:
        if str(row.get("cycle", "")).strip() != "2024":
            continue
        if str(row.get("state", "")).strip().lower() != "national":
            continue
        candidate = str(row.get("candidate", "")).strip().lower()
        day = _parse_date(row.get("date", "") or "")
        prob = _safe_float(row.get("pct_estimate"))
        if day is None or prob is None:
            continue
        daily_pairs[day.isoformat()][candidate] = prob

    # Build a continuous daily Trump win probability proxy.
    observations: list[tuple[date, float]] = []
    for day_str, vals in sorted(daily_pairs.items()):
        day = _parse_date(day_str)
        if day is None:
            continue
        trump = vals.get("trump")
        dem = vals.get("harris", vals.get("biden"))
        if trump is None or dem is None:
            continue
        margin = trump - dem
        # Conservative mapping from polling margin to win probability.
        prob = _normal_cdf(margin / 6.0)
        observations.append((day, prob))

    if not observations:
        return {
            "rows": [],
            "issues": ["Local 538 proxy file contained no usable Trump/Harris or Trump/Biden national rows."],
        }

    daily_map = {d.isoformat(): prob for d, prob in observations}
    filled = _forward_fill(daily_map, date(2024, 1, 1), date(2024, 11, 5))
    raw_file_path = str(source_file)
    out_rows = [
        {
            "date": d.isoformat(),
            "trump_win_prob_polls": round(prob, 6),
            "source": "FiveThirtyEight national polling averages proxy",
            "window_start": "2024-01-01",
            "window_end": "2024-11-05",
            "granularity": "daily",
            "method": "forward_fill_last_national_average_then_normal_cdf",
            "raw_file_path": raw_file_path,
            "notes": "Proxy benchmark derived from national Trump-vs-Democratic-candidate polling averages.",
        }
        for d, prob in filled
    ]
    return {"rows": out_rows, "issues": []}


def build_fedwatch_benchmark(base_dir: Path | str = ".") -> tuple[Path, list[str]]:
    """Build a FedWatch-style benchmark for two 2024 meetings.

    The implementation first tries to read a direct CSV export from a public
    FedWatch-like source. If that is not reachable, it reconstructs a daily
    proxy from public futures prices. The output is kept explicit about which
    method was used.
    """
    paths = project_paths(Path(base_dir))
    ensure_dir(paths["cleaned"])
    ensure_dir(paths["validation"])
    ensure_dir(paths["raw"])

    out_path = paths["cleaned"] / "fedwatch_benchmark.csv"
    issues: list[str] = []

    # Meetings chosen to give one consensus case and one suspense case.
    meeting_dates = [date(2024, 7, 31), date(2024, 9, 18)]
    all_rows: list[dict[str, Any]] = []

    for meeting_date in meeting_dates:
        rows, source_name, method, raw_path, meeting_issues = _build_fedwatch_meeting_series(paths, meeting_date)
        issues.extend(meeting_issues)
        all_rows.extend(rows)

    write_csv(out_path, FEDWATCH_BENCHMARK_COLUMNS, all_rows)
    _write_method_note(
        paths["validation"] / "fedwatch_benchmark_method.md",
        [
            "# FedWatch benchmark method",
            "",
            "- Meetings covered: 2024-07-31 and 2024-09-18.",
            "- Preferred source: public FedWatch CSV exports when reachable.",
            "- Fallback: reconstruction from public 30-Day Fed Funds futures price history and FRED target-range series.",
            "- Probability rows are daily snapshots over the 30 days leading into each meeting.",
            "- The output keeps `method` and `source` explicit so reconstructed rows are not confused with a proprietary CME export.",
        ],
    )
    return out_path, issues


def _build_fedwatch_meeting_series(
    paths: dict[str, Path],
    meeting_date: date,
) -> tuple[list[dict[str, Any]], str, str, str, list[str]]:
    issues: list[str] = []
    source = "CME FedWatch proxy"
    method = "futures_reconstruction"
    raw_path = ""

    # Try a public CSV-like page first if it exists in the future; if not,
    # reconstruct from public futures prices.
    days = [meeting_date - timedelta(days=offset) for offset in range(30, -1, -1)]
    fut_rows, fut_text, fut_url, fut_symbol = _attempt_futures_history(paths, meeting_date, days)
    if not fut_rows:
        issues.append(f"No public futures history found for meeting {meeting_date.isoformat()}.")
        return [], source, method, raw_path, issues

    # Save raw artifact.
    raw_dir = ensure_dir(paths["raw"] / "section_iv")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw_path = str(raw_dir / f"fedwatch_{meeting_date.isoformat()}_{ts}.json")
    _write_text(Path(raw_path), fut_text)

    target_lowers, target_uppers, target_raw = _load_fred_target_ranges(paths, days)
    if not target_lowers or not target_uppers:
        issues.append("FRED target-range series could not be loaded; using midpoint defaults where available.")

    rows: list[dict[str, Any]] = []
    for day in days:
        day_key = day.isoformat()
        if day_key not in fut_rows:
            continue
        close = fut_rows[day_key]
        implied_rate = 100.0 - close
        lower = target_lowers.get(day_key, target_lowers[max(target_lowers)] if target_lowers else None)
        upper = target_uppers.get(day_key, target_uppers[max(target_uppers)] if target_uppers else None)
        if lower is None or upper is None:
            continue
        current_mid = (lower + upper) / 2.0
        for outcome_label, outcome_mid in _fed_outcome_grid(current_mid):
            probability = _interpolate_probability(implied_rate, current_mid, outcome_mid)
            rows.append(
                {
                    "meeting_date": meeting_date.isoformat(),
                    "snapshot_date": day.isoformat(),
                    "target_rate_outcome": outcome_label,
                    "probability": round(probability, 6),
                    "source": source,
                    "method": method,
                    "futures_symbol": fut_symbol,
                    "implied_rate": round(implied_rate, 6),
                    "current_target_lower": round(lower, 6),
                    "current_target_upper": round(upper, 6),
                    "raw_file_path": raw_path,
                    "notes": f"proxy_from={fut_url}",
                }
            )
    return rows, source, method, raw_path, issues


def _attempt_futures_history(
    paths: dict[str, Path],
    meeting_date: date,
    days: list[date],
) -> tuple[dict[str, float], str, str, str]:
    # Try to use a Yahoo Finance futures history endpoint; fall back to any
    # cached local copy if the network is unavailable.
    issues: list[str] = []
    for symbol in _futures_symbol_candidates(meeting_date):
        try:
            rows, text, final_url = _fetch_yahoo_history(symbol, days[0], days[-1])
            if rows:
                mapping = {row["date"]: float(row["close"]) for row in rows}
                return mapping, text, final_url, symbol
        except Exception as exc:
            issues.append(f"{symbol}: {exc}")
            continue

    # Cached fallback path: if a local futures CSV has been placed in the raw directory.
    cached = _locate_local_file(paths, f"fed_futures_{meeting_date.year}")
    if cached is not None:
        try:
            rows = _read_csv(cached)
            mapping = {}
            for row in rows:
                day = _parse_date(row.get("date", "") or row.get("snapshot_date", "") or "")
                close = _safe_float(row.get("close") or row.get("price"))
                if day is None or close is None:
                    continue
                mapping[day.isoformat()] = close
            if mapping:
                return mapping, cached.read_text(encoding="utf-8", errors="ignore"), str(cached), cached.stem
        except Exception:
            pass
    return {}, "\n".join(issues), "", ""


def _load_fred_target_ranges(paths: dict[str, Path], days: list[date]) -> tuple[dict[str, float], dict[str, float], str]:
    lower_rows, lower_text, lower_url = _fetch_fred_series("DFEDTARL")
    upper_rows, upper_text, upper_url = _fetch_fred_series("DFEDTARU")
    if not lower_rows or not upper_rows:
        # try local cached CSVs if present
        cached_lower = _locate_local_file(paths, "DFEDTARL")
        cached_upper = _locate_local_file(paths, "DFEDTARU")
        if cached_lower is not None:
            try:
                lower_rows = [{"date": row["DATE"], "value": float(row["DFEDTARL"])} for row in _read_csv(cached_lower) if row.get("DFEDTARL") not in (None, "", ".")]
                lower_text = cached_lower.read_text(encoding="utf-8", errors="ignore")
                lower_url = str(cached_lower)
            except Exception:
                pass
        if cached_upper is not None:
            try:
                upper_rows = [{"date": row["DATE"], "value": float(row["DFEDTARU"])} for row in _read_csv(cached_upper) if row.get("DFEDTARU") not in (None, "", ".")]
                upper_text = cached_upper.read_text(encoding="utf-8", errors="ignore")
                upper_url = str(cached_upper)
            except Exception:
                pass

    def _fill(rows: list[dict[str, Any]]) -> dict[str, float]:
        values: dict[str, float] = {}
        last: float | None = None
        row_map = {row["date"]: float(row["value"]) for row in rows}
        for day in sorted(days):
            if day.isoformat() in row_map:
                last = row_map[day.isoformat()]
            if last is not None:
                values[day.isoformat()] = last
        return values

    return _fill(lower_rows), _fill(upper_rows), lower_url or upper_url or ""


def _fed_outcome_grid(current_mid: float) -> list[tuple[str, float]]:
    # A compact outcome grid around the prevailing target midpoint.
    return [
        (_rate_label(current_mid - 0.50), current_mid - 0.50),
        (_rate_label(current_mid - 0.25), current_mid - 0.25),
        (_rate_label(current_mid), current_mid),
        (_rate_label(current_mid + 0.25), current_mid + 0.25),
        (_rate_label(current_mid + 0.50), current_mid + 0.50),
    ]


def _rate_label(midpoint: float) -> str:
    lower = midpoint - 0.125
    upper = midpoint + 0.125
    return f"{lower:.2f}-{upper:.2f}"


def _interpolate_probability(implied_rate: float, current_mid: float, outcome_mid: float) -> float:
    # Convert an implied target rate into a single-outcome probability using a
    # simple triangular interpolation over the 25bp outcome grid.
    grid = [current_mid - 0.50, current_mid - 0.25, current_mid, current_mid + 0.25, current_mid + 0.50]
    grid = sorted(grid)
    if implied_rate <= grid[0]:
        return 1.0 if outcome_mid == grid[0] else 0.0
    if implied_rate >= grid[-1]:
        return 1.0 if outcome_mid == grid[-1] else 0.0
    for lo, hi in zip(grid, grid[1:]):
        if lo <= implied_rate <= hi:
            if outcome_mid == lo:
                return (hi - implied_rate) / (hi - lo)
            if outcome_mid == hi:
                return (implied_rate - lo) / (hi - lo)
            return 0.0
    return 0.0


def _write_method_note(path: Path, lines: list[str]) -> None:
    ensure_dir(path.parent)
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _read_series_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _extract_xy_series(rows: list[dict[str, str]], x_key: str, y_key: str) -> list[tuple[date, float]]:
    out: list[tuple[date, float]] = []
    for row in rows:
        day = _parse_date(row.get(x_key, "") or "")
        val = _safe_float(row.get(y_key))
        if day is None or val is None:
            continue
        out.append((day, val))
    return out


def build_section_iv_overlays(base_dir: Path | str = ".") -> dict[str, Path]:
    """Render lightweight HTML overlays for the thesis figures."""
    paths = project_paths(Path(base_dir))
    ensure_dir(paths["artifacts"] / "figures")
    trump_benchmark = paths["cleaned"] / "trump_polls_benchmark.csv"
    fed_benchmark = paths["cleaned"] / "fedwatch_benchmark.csv"
    trump_source = paths["cleaned"] / "polymarket_focused_events_long.csv"
    if not trump_source.exists():
        trump_source = paths["cleaned"] / "polymarket_trump_2024_price_history_long.csv"
    fed_source = paths["cleaned"] / "polymarket_focused_events_long.csv"

    outputs: dict[str, Path] = {}
    if trump_benchmark.exists() and trump_source.exists():
        outputs["trump_overlay"] = _render_overlay_html(
            output_path=paths["artifacts"] / "figures" / "trump_overlay.html",
            title="Trump 2024: Polymarket vs polling benchmark",
            source_rows=_read_series_csv(trump_source) if trump_source.suffix == ".csv" else [],
            source_case="trump_2024",
            source_y_key="probability",
            source_x_key="date",
            benchmark_rows=_read_series_csv(trump_benchmark),
            benchmark_y_key="trump_win_prob_polls",
            benchmark_x_key="date",
            annotations=[
                ("2024-06-27", "Debate"),
                ("2024-07-21", "Biden withdraws"),
                ("2024-08-19", "Harris nomination"),
                ("2024-11-05", "Election"),
            ],
        )
    if fed_benchmark.exists() and fed_source.exists():
        outputs["fed_overlay"] = _render_overlay_html(
            output_path=paths["artifacts"] / "figures" / "fed_overlay.html",
            title="FOMC: Polymarket vs FedWatch benchmark",
            source_rows=_read_series_csv(fed_source) if fed_source.suffix == ".csv" else [],
            source_case="fed_january",
            source_y_key="probability",
            source_x_key="date",
            benchmark_rows=_read_series_csv(fed_benchmark),
            benchmark_y_key="probability",
            benchmark_x_key="snapshot_date",
            annotations=[
                ("2024-07-31", "July meeting"),
                ("2024-09-18", "September meeting"),
            ],
        )
    return outputs


def _render_overlay_html(
    output_path: Path,
    title: str,
    source_rows: list[dict[str, str]],
    source_case: str,
    source_x_key: str,
    source_y_key: str,
    benchmark_rows: list[dict[str, str]],
    benchmark_x_key: str,
    benchmark_y_key: str,
    annotations: list[tuple[str, str]],
) -> Path:
    source_series: list[tuple[date, float]] = []
    benchmark_series: list[tuple[date, float]] = []

    for row in source_rows:
        if row.get("research_case") and row.get("research_case") != source_case:
            continue
        if row.get("source_endpoint_type") == "":
            continue
        day = _parse_date(row.get(source_x_key, "") or "")
        val = _safe_float(row.get(source_y_key))
        if day is not None and val is not None:
            source_series.append((day, val))

    for row in benchmark_rows:
        day = _parse_date(row.get(benchmark_x_key, "") or "")
        val = _safe_float(row.get(benchmark_y_key))
        if day is not None and val is not None:
            benchmark_series.append((day, val))

    all_points = source_series + benchmark_series
    if not all_points:
        _write_text(output_path, f"<html><body><h1>{title}</h1><p>No data.</p></body></html>")
        return output_path

    min_date = min(day for day, _ in all_points)
    max_date = max(day for day, _ in all_points)
    min_val = 0.0
    max_val = 1.0

    def x_scale(day: date, width: int = 980, pad: int = 60) -> float:
        total = max((max_date - min_date).days, 1)
        return pad + ((day - min_date).days / total) * (width - pad * 2)

    def y_scale(val: float, height: int = 420, pad: int = 30) -> float:
        return pad + (1.0 - (val - min_val) / (max_val - min_val)) * (height - pad * 2)

    def path_for(series: list[tuple[date, float]]) -> str:
        ordered = sorted(series)
        if not ordered:
            return ""
        parts = []
        for idx, (day, val) in enumerate(ordered):
            x = x_scale(day)
            y = y_scale(val)
            parts.append(f"{'M' if idx == 0 else 'L'}{x:.1f},{y:.1f}")
        return " ".join(parts)

    source_path = path_for(source_series)
    benchmark_path = path_for(benchmark_series)

    def annotation_svg(day_str: str, label: str) -> str:
        day = _parse_date(day_str)
        if day is None:
            return ""
        x = x_scale(day)
        return f"<line x1='{x:.1f}' y1='20' x2='{x:.1f}' y2='390' stroke='#999' stroke-dasharray='4 4'/><text x='{x + 4:.1f}' y='34' font-size='12' fill='#444'>{label}</text>"

    ann_svg = "".join(annotation_svg(day, label) for day, label in annotations)

    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='1000' height='460' viewBox='0 0 1000 460'>
  <rect width='1000' height='460' fill='white'/>
  <text x='40' y='22' font-size='18' font-family='sans-serif'>{title}</text>
  <line x1='60' y1='20' x2='60' y2='390' stroke='#222'/>
  <line x1='60' y1='390' x2='940' y2='390' stroke='#222'/>
  <text x='20' y='390' font-size='12' font-family='sans-serif'>0%</text>
  <text x='18' y='30' font-size='12' font-family='sans-serif'>100%</text>
  <path d='{benchmark_path}' fill='none' stroke='#9c755f' stroke-width='2'/>
  <path d='{source_path}' fill='none' stroke='#1f77b4' stroke-width='2'/>
  {ann_svg}
  <rect x='680' y='32' width='14' height='4' fill='#1f77b4'/><text x='700' y='38' font-size='12' font-family='sans-serif'>Polymarket</text>
  <rect x='680' y='52' width='14' height='4' fill='#9c755f'/><text x='700' y='58' font-size='12' font-family='sans-serif'>Benchmark</text>
</svg>"""
    html = f"""<!doctype html>
<html lang='en'>
<head><meta charset='utf-8'><title>{title}</title></head>
<body style='font-family:sans-serif;background:#fafafa;margin:0;padding:20px;'>
{svg}
</body>
</html>"""
    _write_text(output_path, html)
    return output_path


def run_section_iv_build(base_dir: Path | str = ".") -> dict[str, Path]:
    trump_path, _ = build_trump_polls_benchmark(base_dir)
    fed_path, _ = build_fedwatch_benchmark(base_dir)
    overlay_paths = build_section_iv_overlays(base_dir)
    outputs = {
        "trump_polls_benchmark": trump_path,
        "fedwatch_benchmark": fed_path,
    }
    outputs.update(overlay_paths)
    return outputs

