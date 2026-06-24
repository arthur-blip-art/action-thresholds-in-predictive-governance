"""Convenience wrapper for the final thesis delivery build."""

from __future__ import annotations

from pathlib import Path

from forecast_pipeline.final_thesis_delivery import run_final_thesis_delivery


if __name__ == "__main__":
    outputs = run_final_thesis_delivery(Path(__file__).resolve().parent)
    for name, path in outputs.items():
        print(f"{name}: {path}")
    raise SystemExit(0)

