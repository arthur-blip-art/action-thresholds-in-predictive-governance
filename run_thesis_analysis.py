"""Convenience wrapper for the Polymarket thesis analysis build."""

from __future__ import annotations

from pathlib import Path

from forecast_pipeline.thesis_analysis import run_thesis_analysis


if __name__ == "__main__":
    outputs = run_thesis_analysis(Path(__file__).resolve().parent)
    for name, path in outputs.items():
        print(f"{name}: {path}")
    raise SystemExit(0)
