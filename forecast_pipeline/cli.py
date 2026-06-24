"""Command line entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

from .pipeline import ForecastPipeline
from .fed_multi_event_analysis import run_fed_multi_event_analysis
from .polymarket_focus import polymarket_event_spec, run_polymarket_event
from .polymarket_focused_consolidation import run_polymarket_focused_consolidation
from .thesis_analysis import run_thesis_analysis
from .section_iv import run_section_iv_build
from .fed_dec_to_apr_analysis import run_fed_dec_to_apr_analysis
from .final_thesis_delivery import run_final_thesis_delivery


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the forecast data pipeline.")
    parser.add_argument("--base-dir", default=".", help="Project root containing inputs/ and outputs.")
    parser.add_argument("--live", action="store_true", help="Use public live collectors instead of local scaffold inputs.")
    parser.add_argument("--focused", action="store_true", help="Limit live collection to exact research cases and small source traces.")
    parser.add_argument(
        "--source",
        choices=["polymarket", "kalshi", "election_benchmarks", "cme_fedwatch", "anthropic_references"],
        help="Run only one live source collector.",
    )
    parser.add_argument(
        "--event",
        choices=["anthropic_valuation", "trump_2024", "fed_january"],
        help="Run a single focused Polymarket event.",
    )
    parser.add_argument(
        "--consolidate-polymarket-focused",
        action="store_true",
        help="Build the unified Polymarket-only dataset, daily aggregate, snapshots, and validation report.",
    )
    parser.add_argument(
        "--section-iv",
        action="store_true",
        help="Build the Section IV benchmarks and overlays.",
    )
    parser.add_argument(
        "--thesis-analysis",
        action="store_true",
        help="Build the Polymarket thesis analysis outputs under Data/analysis.",
    )
    parser.add_argument(
        "--fed-multi-event",
        action="store_true",
        help="Build the repeated Fed decision Polymarket analysis outputs.",
    )
    parser.add_argument(
        "--fed-dec-to-apr",
        action="store_true",
        help="Build the Fed Dec-to-Apr realized analysis outputs.",
    )
    parser.add_argument(
        "--final-thesis-delivery",
        action="store_true",
        help="Build the final thesis figures and interpretation notes from the existing outputs.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.fed_multi_event:
        outputs = run_fed_multi_event_analysis(Path(args.base_dir))
        for name, path in outputs.items():
            print(f"{name}: {path}")
        return 0
    if args.fed_dec_to_apr:
        outputs = run_fed_dec_to_apr_analysis(Path(args.base_dir))
        for name, path in outputs.items():
            print(f"{name}: {path}")
        return 0
    if args.final_thesis_delivery:
        outputs = run_final_thesis_delivery(Path(args.base_dir))
        for name, path in outputs.items():
            print(f"{name}: {path}")
        return 0
    if args.thesis_analysis:
        outputs = run_thesis_analysis(Path(args.base_dir))
        for name, path in outputs.items():
            print(f"{name}: {path}")
        return 0
    if args.consolidate_polymarket_focused:
        outputs = run_polymarket_focused_consolidation(Path(args.base_dir))
        for name, path in outputs.items():
            print(f"{name}: {path}")
        return 0
    if args.section_iv:
        outputs = run_section_iv_build(Path(args.base_dir))
        for name, path in outputs.items():
            print(f"{name}: {path}")
        return 0
    if args.live and args.source == "polymarket" and args.event:
        spec = polymarket_event_spec(args.event)
        outputs = run_polymarket_event(Path(args.base_dir), spec)
        for name, path in outputs.items():
            print(f"{name}: {path}")
        return 0
    pipeline = ForecastPipeline(Path(args.base_dir), live=args.live, focused=args.focused, source_filter=args.source)
    outputs = pipeline.run()
    for name, path in outputs.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
