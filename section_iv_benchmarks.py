"""Standalone Section IV benchmark builder.

This script avoids importing ``forecast_pipeline`` so it can still be used even
if the package directory becomes inaccessible in the current environment.

It builds:
- ``pipeline_outputs/cleaned/trump_polls_benchmark.csv``
- ``pipeline_outputs/cleaned/fedwatch_benchmark.csv``
- simple HTML overlays in ``pipeline_outputs/figures/``

The implementation is deliberately conservative:
- prefer public downloadable data when reachable
- fall back to transparent proxy series only when explicitly documented
- never invent probabilities without labeling the method used
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


TRUMP_COLUMNS = (
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

FED_COLUMNS = (
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


def _project_root(base_dir: str | Path = ".") -> Path:
    return Path(base_dir).resolve()


def _paths(base_dir: str | Path = ".") -> dict[str, Path]:
    root = _project_root(base_dir)
    outputs = root / "pipeline_outputs"
    return {
        "root": root,
        "outputs": outputs,
        "raw": outputs / "raw",
        "cleaned": outputs / "cleaned",
        "validation": outputs / "validation",
        "figures": outputs / "figures",
    }


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    _ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if v is None else v for k, v in row.items()})


def _write_text(path: Path, text: str) -> None:
    _ensure_dir(path.parent)
    path.write_text(text, encoding="utf-8")


def _parse_date(value: str) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
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


def _fetch_text(url: str, params: dict[str, Any] | None = None, timeout: int = 20) -> tuple[str, int, str]:
    resp = requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0 (Codex thesis pipeline)",
            "Accept": "text/html,application/json,text/csv,*/*",
        },
    )
    return resp.text, resp.status_code, resp.url


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _latest_by_day(rows: list[dict[str, str]], date_key: str, value_key: str) -> dict[str, float]:
    latest: dict[str, tuple[date, float]] = {}
    for row in rows:
        day = _parse_date(row.get(date_key, "") or "")
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


def build_trump_polls_benchmark(base_dir: str | Path = ".") -> tuple[Path, list[str]]:
    paths = _paths(base_dir)
    _ensure_dir(paths["cleaned"])
    _ensure_dir(paths["validation"])
    _ensure_dir(paths["raw"])

    out_path = paths["cleaned"] / "trump_polls_benchmark.csv"
    issues: list[str] = []
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path, issues

    # Preferred public archive if reachable.
    source_url = "https://projects.jhkforecasts.com/forecast-history/output.csv"
    try:
        text, status, final_url = _fetch_text(source_url)
        if status == 200 and text.strip() and "," in text.splitlines()[0]:
            raw_dir = _ensure_dir(paths["raw"] / "section_iv")
            raw_path = raw_dir / f"trump_forecast_history_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.csv"
            _write_text(raw_path, text)
            reader = csv.DictReader(text.splitlines())
            daily: dict[str, float] = {}
            source_rows: list[dict[str, Any]] = []
            for row in reader:
                if "2024" not in " ".join(str(v) for v in row.values()).lower():
                    continue
                day = None
                for key in ("date", "forecast_date", "snapshot_date", "updated", "timestamp"):
                    day = _parse_date(row.get(key, "") or "")
                    if day is not None:
                        break
                prob = None
                for key in ("trump_win_prob", "win_prob", "probability", "forecast_probability", "prob", "chance"):
                    prob = _safe_float(row.get(key))
                    if prob is not None:
                        break
                if day is None or prob is None:
                    continue
                if prob > 1.0:
                    prob = prob / 100.0
                daily[day.isoformat()] = prob
                source_rows.append({"date": day.isoformat(), "prob": prob, "raw": raw_path, "url": final_url})
            if source_rows:
                filled = _forward_fill(daily, date(2024, 1, 1), date(2024, 11, 5))
                first_obs = min(_parse_date(row["date"]) for row in source_rows if _parse_date(row["date"]))
                rows = [
                    {
                        "date": d.isoformat(),
                        "trump_win_prob_polls": round(prob, 6),
                        "source": "Silver Bulletin / forecast-history",
                        "window_start": first_obs.isoformat() if first_obs else "2024-01-01",
                        "window_end": "2024-11-05",
                        "granularity": "daily",
                        "method": "direct_forecast_history_forward_fill",
                        "raw_file_path": str(raw_path),
                        "notes": f"direct_source={final_url}",
                    }
                    for d, prob in filled
                ]
                _write_csv(out_path, TRUMP_COLUMNS, rows)
                _write_text(
                    paths["validation"] / "trump_polls_benchmark_method.md",
                    "\n".join(
                        [
                            "# Trump 2024 benchmark method",
                            "",
                            "- Preferred source: Silver Bulletin / forecast-history export when reachable.",
                            "- Daily rows forward-fill from the first observed forecast through 2024-11-05.",
                            "- If the preferred source is unreachable, the build falls back to a transparent FiveThirtyEight proxy.",
                        ]
                    )
                    + "\n",
                )
                return out_path, issues
    except Exception as exc:
        issues.append(f"direct forecast-history fetch failed: {exc}")

    # Transparent local proxy from the local FiveThirtyEight archive captured in pipeline outputs.
    local_source = None
    for candidate in sorted((paths["raw"] / "fivethirtyeight").glob("*presidential-general-averages-2024-09-12-uncorrected-csv.txt")):
        local_source = candidate
    if local_source is None:
        issues.append("No local FiveThirtyEight presidential-general averages file found.")
        _write_csv(out_path, TRUMP_COLUMNS, [])
        _write_text(
            paths["validation"] / "trump_polls_benchmark_method.md",
            "\n".join(
                [
                    "# Trump 2024 benchmark method",
                    "",
                    "- No direct archive or local FiveThirtyEight proxy file was reachable in this environment.",
                    "- The CSV is empty rather than fabricated.",
                ]
            )
            + "\n",
        )
        return out_path, issues

    try:
        rows = _read_csv(local_source)
    except Exception as exc:
        issues.append(f"Local FiveThirtyEight proxy file could not be read: {exc}")
        _write_csv(out_path, TRUMP_COLUMNS, [])
        _write_text(
            paths["validation"] / "trump_polls_benchmark_method.md",
            "\n".join(
                [
                    "# Trump 2024 benchmark method",
                    "",
                    "- The local FiveThirtyEight proxy file exists but could not be read in this environment.",
                    "- The CSV is empty rather than fabricated.",
                ]
            )
            + "\n",
        )
        return out_path, issues
    daily_pairs: dict[str, dict[str, float]] = {}
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
        daily_pairs.setdefault(day.isoformat(), {})[candidate] = prob

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
        observations.append((day, _normal_cdf(margin / 6.0)))

    if not observations:
        issues.append("The local FiveThirtyEight proxy file contained no usable Trump/Harris or Trump/Biden rows.")
        _write_csv(out_path, TRUMP_COLUMNS, [])
        return out_path, issues

    first_obs = min(day for day, _ in observations)
    daily_map = {day.isoformat(): prob for day, prob in observations}
    filled = _forward_fill(daily_map, first_obs, date(2024, 11, 5))
    proxy_rows = [
        {
            "date": d.isoformat(),
            "trump_win_prob_polls": round(prob, 6),
            "source": "FiveThirtyEight national polling averages proxy",
            "window_start": first_obs.isoformat(),
            "window_end": "2024-11-05",
            "granularity": "daily",
            "method": "forward_fill_last_national_average_then_normal_cdf",
            "raw_file_path": str(local_source),
            "notes": f"Proxy benchmark derived from national Trump-vs-Democratic-candidate polling averages; first observed row={first_obs.isoformat()}.",
        }
        for d, prob in filled
    ]
    _write_csv(out_path, TRUMP_COLUMNS, proxy_rows)
    _write_text(
        paths["validation"] / "trump_polls_benchmark_method.md",
        "\n".join(
            [
                "# Trump 2024 benchmark method",
                "",
                "- Fallback source: FiveThirtyEight 2024 national presidential averages captured locally in pipeline_outputs/raw/fivethirtyeight.",
                "- Benchmark probability is a proxy derived from the Trump-minus-Democratic-candidate national polling margin.",
                "- The proxy uses a standard-normal mapping so the series remains bounded in [0, 1].",
                "- Daily rows are forward-filled from the first available 2024 national observation through 2024-11-05.",
            ]
        )
        + "\n",
    )
    return out_path, issues


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
    try:
        text, status, final_url = _fetch_text(url, params=params, timeout=15)
    except Exception as exc:
        return [], str(exc), url
    if status != 200:
        return [], text, final_url
    try:
        payload = json.loads(text)
        result = payload["chart"]["result"][0]
        timestamps = result.get("timestamp", [])
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        closes = quote.get("close", [])
        rows: list[dict[str, Any]] = []
        for ts, close in zip(timestamps, closes):
            if close in (None, ""):
                continue
            rows.append({"timestamp": int(ts), "date": datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat(), "close": float(close)})
        return rows, text, final_url
    except Exception:
        return [], text, final_url


def _fetch_fred_series(series_id: str) -> tuple[list[dict[str, Any]], str, str]:
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    params = {"id": series_id}
    try:
        text, status, final_url = _fetch_text(url, params=params, timeout=15)
    except Exception as exc:
        return [], str(exc), url
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


def _load_fred_target_ranges(days: list[date]) -> tuple[dict[str, float], dict[str, float]]:
    lower_rows, _, _ = _fetch_fred_series("DFEDTARL")
    upper_rows, _, _ = _fetch_fred_series("DFEDTARU")
    lower_map = {row["date"]: float(row["value"]) for row in lower_rows}
    upper_map = {row["date"]: float(row["value"]) for row in upper_rows}

    def _fill(rows: dict[str, float]) -> dict[str, float]:
        values: dict[str, float] = {}
        last: float | None = None
        for day in sorted(days):
            if day.isoformat() in rows:
                last = rows[day.isoformat()]
            if last is not None:
                values[day.isoformat()] = last
        return values

    return _fill(lower_map), _fill(upper_map)


def _rate_label(midpoint: float) -> str:
    lower = midpoint - 0.125
    upper = midpoint + 0.125
    return f"{lower:.2f}-{upper:.2f}"


def _fed_outcome_grid(current_mid: float) -> list[tuple[str, float]]:
    return [
        (_rate_label(current_mid - 0.50), current_mid - 0.50),
        (_rate_label(current_mid - 0.25), current_mid - 0.25),
        (_rate_label(current_mid), current_mid),
        (_rate_label(current_mid + 0.25), current_mid + 0.25),
        (_rate_label(current_mid + 0.50), current_mid + 0.50),
    ]


def _interpolate_probability(implied_rate: float, current_mid: float, outcome_mid: float) -> float:
    grid = sorted([current_mid - 0.50, current_mid - 0.25, current_mid, current_mid + 0.25, current_mid + 0.50])
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


def build_fedwatch_benchmark(base_dir: str | Path = ".") -> tuple[Path, list[str]]:
    paths = _paths(base_dir)
    _ensure_dir(paths["cleaned"])
    _ensure_dir(paths["validation"])
    _ensure_dir(paths["raw"])

    out_path = paths["cleaned"] / "fedwatch_benchmark.csv"
    issues: list[str] = []
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path, issues
    meeting_dates = [date(2024, 7, 31), date(2024, 9, 18)]
    rows: list[dict[str, Any]] = []

    for meeting_date in meeting_dates:
        days = [meeting_date - timedelta(days=offset) for offset in range(30, -1, -1)]
        fut_map: dict[str, float] = {}
        fut_text = ""
        fut_url = ""
        fut_symbol = ""
        for symbol in _futures_symbol_candidates(meeting_date):
            fut_rows, fut_text_candidate, fut_url_candidate = _fetch_yahoo_history(symbol, days[0], days[-1])
            if fut_rows:
                fut_map = {row["date"]: float(row["close"]) for row in fut_rows}
                fut_text = fut_text_candidate
                fut_url = fut_url_candidate
                fut_symbol = symbol
                break

        if not fut_map:
            issues.append(f"No public futures history found for meeting {meeting_date.isoformat()}.")
            continue

        raw_dir = _ensure_dir(paths["raw"] / "section_iv")
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        raw_path = raw_dir / f"fedwatch_{meeting_date.isoformat()}_{ts}.json"
        _write_text(raw_path, fut_text)

        target_lowers, target_uppers = _load_fred_target_ranges(days)
        if not target_lowers or not target_uppers:
            issues.append("FRED target-range series could not be loaded; using midpoint defaults where available.")

        for day in days:
            day_key = day.isoformat()
            if day_key not in fut_map:
                continue
            close = fut_map[day_key]
            implied_rate = 100.0 - close
            lower = target_lowers.get(day_key)
            upper = target_uppers.get(day_key)
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
                        "source": "CME FedWatch proxy",
                        "method": "futures_reconstruction",
                        "futures_symbol": fut_symbol,
                        "implied_rate": round(implied_rate, 6),
                        "current_target_lower": round(lower, 6),
                        "current_target_upper": round(upper, 6),
                        "raw_file_path": str(raw_path),
                        "notes": f"proxy_from={fut_url}",
                    }
                )

    _write_csv(out_path, FED_COLUMNS, rows)
    _write_text(
        paths["validation"] / "fedwatch_benchmark_method.md",
        "\n".join(
            [
                "# FedWatch benchmark method",
                "",
                "- Meetings covered: 2024-07-31 and 2024-09-18.",
                "- Preferred source: public FedWatch CSV exports when reachable.",
                "- Fallback: reconstruction from public 30-Day Fed Funds futures price history and FRED target-range series.",
                "- Probability rows are daily snapshots over the 30 days leading into each meeting.",
                "- If the environment cannot reach the public inputs, the CSV is written empty and the issue list explains why.",
            ]
        )
        + "\n",
    )
    return out_path, issues


def _extract_series_rows(csv_path: Path, x_key: str, y_key: str, filter_key: str | None = None, filter_value: str | None = None) -> list[tuple[date, float]]:
    try:
        rows = _read_csv(csv_path)
    except Exception:
        return []
    out: list[tuple[date, float]] = []
    for row in rows:
        if filter_key and filter_value and row.get(filter_key) != filter_value:
            continue
        day = _parse_date(row.get(x_key, "") or "")
        val = _safe_float(row.get(y_key))
        if day is None or val is None:
            continue
        out.append((day, val))
    return out


def _render_overlay_html(
    output_path: Path,
    title: str,
    source_series: list[tuple[date, float]],
    benchmark_series: list[tuple[date, float]],
    annotations: list[tuple[str, str]],
    source_label: str = "Polymarket",
    benchmark_label: str = "Benchmark",
) -> Path:
    all_points = source_series + benchmark_series
    if not all_points:
        _write_text(output_path, f"<html><body><h1>{title}</h1><p>No data.</p></body></html>")
        return output_path

    min_date = min(day for day, _ in all_points)
    max_date = max(day for day, _ in all_points)

    def x_scale(day: date, width: int = 980, pad: int = 60) -> float:
        total = max((max_date - min_date).days, 1)
        return pad + ((day - min_date).days / total) * (width - pad * 2)

    def y_scale(val: float, height: int = 420, pad: int = 30) -> float:
        return pad + (1.0 - val) * (height - pad * 2)

    def path_for(series: list[tuple[date, float]]) -> str:
        ordered = sorted(series)
        if not ordered:
            return ""
        parts: list[str] = []
        for idx, (day, val) in enumerate(ordered):
            x = x_scale(day)
            y = y_scale(val)
            parts.append(f"{'M' if idx == 0 else 'L'}{x:.1f},{y:.1f}")
        return " ".join(parts)

    def annotation_svg(day_str: str, label: str) -> str:
        day = _parse_date(day_str)
        if day is None:
            return ""
        x = x_scale(day)
        return f"<line x1='{x:.1f}' y1='20' x2='{x:.1f}' y2='390' stroke='#999' stroke-dasharray='4 4'/><text x='{x + 4:.1f}' y='34' font-size='12' fill='#444'>{label}</text>"

    svg = f"""<svg xmlns='http://www.w3.org/2000/svg' width='1000' height='460' viewBox='0 0 1000 460'>
  <rect width='1000' height='460' fill='white'/>
  <text x='40' y='22' font-size='18' font-family='sans-serif'>{title}</text>
  <line x1='60' y1='20' x2='60' y2='390' stroke='#222'/>
  <line x1='60' y1='390' x2='940' y2='390' stroke='#222'/>
  <text x='20' y='390' font-size='12' font-family='sans-serif'>0%</text>
  <text x='18' y='30' font-size='12' font-family='sans-serif'>100%</text>
  <path d='{path_for(benchmark_series)}' fill='none' stroke='#9c755f' stroke-width='2'/>
  <path d='{path_for(source_series)}' fill='none' stroke='#1f77b4' stroke-width='2'/>
  {''.join(annotation_svg(day, label) for day, label in annotations)}
  <rect x='680' y='32' width='14' height='4' fill='#1f77b4'/><text x='700' y='38' font-size='12' font-family='sans-serif'>{source_label}</text>
  <rect x='680' y='52' width='14' height='4' fill='#9c755f'/><text x='700' y='58' font-size='12' font-family='sans-serif'>{benchmark_label}</text>
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


def build_overlays(base_dir: str | Path = ".") -> dict[str, Path]:
    paths = _paths(base_dir)
    _ensure_dir(paths["figures"])

    trump_benchmark = paths["cleaned"] / "trump_polls_benchmark.csv"
    fed_benchmark = paths["cleaned"] / "fedwatch_benchmark.csv"
    trump_source = paths["cleaned"] / "polymarket_trump_2024_price_history_long.csv"
    fed_source = paths["cleaned"] / "polymarket_focused_events_long.csv"

    outputs: dict[str, Path] = {}
    if trump_benchmark.exists() and trump_source.exists():
        outputs["trump_overlay"] = _render_overlay_html(
            output_path=paths["figures"] / "trump_overlay.html",
            title="Trump 2024: Polymarket vs polling benchmark",
            source_series=_extract_series_rows(trump_source, "date", "probability"),
            benchmark_series=_extract_series_rows(trump_benchmark, "date", "trump_win_prob_polls"),
            annotations=[
                ("2024-06-27", "Debate"),
                ("2024-07-21", "Biden withdraws"),
                ("2024-08-19", "Harris nomination"),
                ("2024-11-05", "Election"),
            ],
        )
    if fed_benchmark.exists() and fed_source.exists():
        outputs["fed_overlay"] = _render_overlay_html(
            output_path=paths["figures"] / "fed_overlay.html",
            title="FOMC: Polymarket vs Fed benchmark",
            source_series=_extract_series_rows(fed_source, "date", "probability", "research_case", "fed_january"),
            benchmark_series=_extract_series_rows(fed_benchmark, "snapshot_date", "probability"),
            annotations=[
                ("2024-07-31", "July meeting"),
                ("2024-09-18", "September meeting"),
            ],
        )
    return outputs


def write_method_notes(base_dir: str | Path = ".") -> None:
    paths = _paths(base_dir)
    _ensure_dir(paths["validation"])
    trump_path = paths["validation"] / "trump_polls_benchmark_method.md"
    fed_path = paths["validation"] / "fedwatch_benchmark_method.md"
    if not trump_path.exists():
        _write_text(trump_path, "# Trump 2024 benchmark method\n\nNo benchmark has been built yet.\n")
    if not fed_path.exists():
        _write_text(fed_path, "# FedWatch benchmark method\n\nNo benchmark has been built yet.\n")


def run_section_iv(base_dir: str | Path = ".") -> dict[str, Path]:
    trump_path, _ = build_trump_polls_benchmark(base_dir)
    fed_path, _ = build_fedwatch_benchmark(base_dir)
    outputs = {"trump_polls_benchmark": trump_path, "fedwatch_benchmark": fed_path}
    outputs.update(build_overlays(base_dir))
    return outputs


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build Section IV benchmark datasets and overlays.")
    parser.add_argument("--base-dir", default=".", help="Project root containing pipeline_outputs/")
    args = parser.parse_args()
    write_method_notes(args.base_dir)
    outputs = run_section_iv(args.base_dir)
    for name, path in outputs.items():
        print(f"{name}: {path}")
