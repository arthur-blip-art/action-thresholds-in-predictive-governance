"""Shared schema helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


CSV_COLUMNS = (
    "source_id",
    "source_name",
    "source_category",
    "source_group",
    "platform",
    "series_kind",
    "benchmark_type",
    "event_id",
    "event_name",
    "category",
    "observation_date",
    "event_date",
    "raw_value",
    "probability_value",
    "benchmark_value",
    "unit",
    "use_in_probability_analysis",
    "data_status",
    "is_real_data",
    "source_endpoint",
    "source_url",
    "raw_file_path",
    "provenance_file",
    "provenance_row",
    "notes",
)


DAILY_COLUMNS = (
    "source_id",
    "source_name",
    "source_category",
    "source_group",
    "platform",
    "series_kind",
    "benchmark_type",
    "event_id",
    "event_name",
    "category",
    "observation_date",
    "event_date",
    "daily_value",
    "daily_value_type",
    "unit",
    "use_in_probability_analysis",
    "data_status",
    "is_real_data",
    "source_endpoint",
    "source_url",
    "raw_file_path",
    "record_count",
    "provenance_file",
    "notes",
)


SNAPSHOT_COLUMNS = (
    "source_id",
    "source_name",
    "source_category",
    "event_id",
    "event_name",
    "category",
    "latest_observation_date",
    "latest_daily_value",
    "data_status",
    "latest_data_status",
    "source_endpoint",
    "source_url",
    "raw_file_path",
    "latest_provenance_file",
    "latest_notes",
)


@dataclass
class Record:
    source_id: str
    source_name: str
    source_category: str
    source_group: str
    platform: str
    series_kind: str
    benchmark_type: str
    event_id: str
    event_name: str
    category: str
    observation_date: str
    event_date: str
    raw_value: str
    probability_value: str
    benchmark_value: str
    unit: str
    use_in_probability_analysis: str
    data_status: str
    is_real_data: str
    source_endpoint: str
    source_url: str
    raw_file_path: str
    provenance_file: str
    provenance_row: str
    notes: str

    def as_row(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "source_category": self.source_category,
            "source_group": self.source_group,
            "platform": self.platform,
            "series_kind": self.series_kind,
            "benchmark_type": self.benchmark_type,
            "event_id": self.event_id,
            "event_name": self.event_name,
            "category": self.category,
            "observation_date": self.observation_date,
            "event_date": self.event_date,
            "raw_value": self.raw_value,
            "probability_value": self.probability_value,
            "benchmark_value": self.benchmark_value,
            "unit": self.unit,
            "use_in_probability_analysis": self.use_in_probability_analysis,
            "data_status": self.data_status,
            "is_real_data": self.is_real_data,
            "source_endpoint": self.source_endpoint,
            "source_url": self.source_url,
            "raw_file_path": self.raw_file_path,
            "provenance_file": self.provenance_file,
            "provenance_row": self.provenance_row,
            "notes": self.notes,
        }


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    return text in {"1", "true", "t", "yes", "y"}


def normalized_bool(value: Any) -> str:
    return "TRUE" if truthy(value) else "FALSE"


def coerce_date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text
