"""Pipeline configuration and source registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    source_name: str
    source_group: str
    series_kind: str
    acquisition_mode: str
    default_data_status: str
    input_files: Sequence[str] = field(default_factory=tuple)
    notes: str = ""

    @property
    def is_probability_series(self) -> bool:
        return self.series_kind == "probability"


DEFAULT_SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        source_id="polymarket",
        source_name="Polymarket",
        source_group="prediction_market",
        series_kind="probability",
        acquisition_mode="downloaded",
        default_data_status="real_downloaded_data",
        input_files=("inputs/raw/polymarket.csv", "inputs/raw/polymarket.json"),
        notes="Public market history when available.",
    ),
    SourceSpec(
        source_id="kalshi",
        source_name="Kalshi",
        source_group="prediction_market",
        series_kind="probability",
        acquisition_mode="api",
        default_data_status="real_api_data",
        input_files=("inputs/raw/kalshi.csv", "inputs/raw/kalshi.json"),
        notes="Public market history when available.",
    ),
    SourceSpec(
        source_id="manifold",
        source_name="Manifold",
        source_group="prediction_market",
        series_kind="probability",
        acquisition_mode="downloaded",
        default_data_status="real_downloaded_data",
        input_files=("inputs/raw/manifold.csv", "inputs/raw/manifold.json"),
        notes="Optional public source.",
    ),
    SourceSpec(
        source_id="cme_fedwatch",
        source_name="CME FedWatch",
        source_group="benchmark",
        series_kind="probability",
        acquisition_mode="downloaded",
        default_data_status="partial",
        input_files=("inputs/raw/cme_fedwatch.csv", "inputs/raw/cme_fedwatch.json"),
        notes="May be partially accessible through CME services.",
    ),
    SourceSpec(
        source_id="election_benchmarks",
        source_name="Election Benchmarks",
        source_group="benchmark",
        series_kind="benchmark",
        acquisition_mode="manual",
        default_data_status="manual_annotation",
        input_files=("inputs/raw/election_benchmarks.csv",),
        notes="Polling-based benchmark rows are not probability series.",
    ),
    SourceSpec(
        source_id="anthropic_references",
        source_name="Anthropic References",
        source_group="qualitative",
        series_kind="qualitative",
        acquisition_mode="manual",
        default_data_status="manual_annotation",
        input_files=("inputs/raw/anthropic_references.csv",),
        notes="Qualitative reference material only.",
    ),
)


def project_paths(base_dir: Path) -> dict[str, Path]:
    root = base_dir.resolve()
    artifacts = root / "pipeline_outputs"
    return {
        "root": root,
        "inputs": root / "inputs",
        "inputs_raw": root / "inputs" / "raw",
        "templates": root / "templates",
        "artifacts": artifacts,
        "raw": artifacts / "raw",
        "cleaned": artifacts / "cleaned",
        "daily": artifacts / "daily",
        "snapshots": artifacts / "snapshots",
        "reports": artifacts / "reports",
        "validation": artifacts / "validation",
        "debug": artifacts / "debug",
        "catalog": artifacts / "catalog",
    }
