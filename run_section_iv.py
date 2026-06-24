"""Convenience wrapper for the standalone Section IV benchmark builder."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent / "section_iv_benchmarks.py"
SPEC = importlib.util.spec_from_file_location("section_iv_benchmarks", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load {MODULE_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
run_section_iv = MODULE.run_section_iv


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Section IV benchmark datasets and overlays.")
    parser.add_argument("--base-dir", default=".", help="Project root containing pipeline_outputs/")
    args = parser.parse_args()
    outputs = run_section_iv(args.base_dir)
    for name, path in outputs.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
