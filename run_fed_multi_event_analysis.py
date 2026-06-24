"""Convenience wrapper for the repeated Fed decision analysis build."""

from __future__ import annotations

from pathlib import Path

from forecast_pipeline.fed_multi_event_analysis import run_fed_multi_event_analysis


if __name__ == "__main__":
    outputs = run_fed_multi_event_analysis(Path(__file__).resolve().parent)
    for name, path in outputs.items():
        print(f"{name}: {path}")
    raise SystemExit(0)

