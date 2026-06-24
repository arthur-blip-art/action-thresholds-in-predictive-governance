"""End-to-end forecast data pipeline."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from statistics import mean
from typing import Iterable

from .config import DEFAULT_SOURCES, SourceSpec, project_paths
from .collectors import (
    AnthropicCollector,
    CmeFedwatchCollector,
    CollectionResult,
    FiveThirtyEightCollector,
    HttpClient,
    KalshiCollector,
    PolymarketCollector,
    RawArtifactWriter,
)
from .io_utils import ensure_dir, read_csv_rows, read_json, write_csv, write_json
from .models import (
    CSV_COLUMNS,
    DAILY_COLUMNS,
    SNAPSHOT_COLUMNS,
    Record,
    coerce_date,
    normalized_bool,
    truthy,
)


class ForecastPipeline:
    """Collect, normalize, aggregate, and validate forecast data."""

    def __init__(
        self,
        base_dir: Path | str = ".",
        sources: Iterable[SourceSpec] = DEFAULT_SOURCES,
        live: bool = False,
        focused: bool = False,
        source_filter: str | None = None,
    ):
        self.base_dir = Path(base_dir)
        self.paths = project_paths(self.base_dir)
        self.sources = tuple(sources)
        self.live = live
        self.focused = focused
        self.source_filter = source_filter

    def run(self) -> dict[str, Path]:
        self._prepare_directories()
        if self.live:
            return self._run_live()
        return self._run_offline()

    def _run_offline(self) -> dict[str, Path]:
        catalog_rows, discovery_report = self.discover_sources()
        write_csv(self.paths["catalog"] / "source_catalog.csv", discovery_report["catalog_columns"], catalog_rows)
        write_json(self.paths["reports"] / "discovery_report.json", discovery_report)

        raw_records = self.ingest_raw_data()
        return self._finalize_outputs(raw_records, discovery_report, [])

    def _run_live(self) -> dict[str, Path]:
        writer = RawArtifactWriter(self.paths["raw"])
        client = HttpClient()
        results: list[CollectionResult] = []
        collectors = self._live_collectors(client, writer)
        print("Live mode: attempting public collectors")
        for source_id, collector in collectors.items():
            print(f"Attempting {source_id}")
            try:
                result = collector.collect()
            except Exception as exc:  # pragma: no cover - defensive logging
                result = CollectionResult(
                    source_id=source_id,
                    source_name=source_id,
                    source_category="unknown",
                    issues=[f"collector_failed: {exc}"],
                )
            print(
                f"{source_id}: {len(result.records)} normalized rows, {len(result.raw_files)} raw files, issues={len(result.issues)}"
            )
            for issue in result.issues:
                print(f"{source_id} issue: {issue}")
            results.append(result)

        raw_records = [record for result in results for record in result.records]
        discovery_report = self._live_discovery_report(results)
        catalog_rows = self._live_catalog_rows(results)
        write_csv(self.paths["catalog"] / "source_catalog.csv", discovery_report["catalog_columns"], catalog_rows)
        write_json(self.paths["reports"] / "discovery_report.json", discovery_report)
        self._write_discovery_markdown(discovery_report)
        return self._finalize_outputs(raw_records, discovery_report, results)

    def _finalize_outputs(
        self,
        raw_records: list[Record],
        discovery_report: dict[str, object],
        results: list[CollectionResult],
    ) -> dict[str, Path]:
        raw_path = self.paths["raw"] / "normalized_raw_records.csv"
        write_csv(raw_path, CSV_COLUMNS, [record.as_row() for record in raw_records])

        cleaned_records = self.clean_records(raw_records)
        cleaned_path = self.paths["cleaned"] / "cleaned_long_form.csv"
        write_csv(cleaned_path, CSV_COLUMNS, [record.as_row() for record in cleaned_records])

        if self.live:
            pm_rows = [record for record in cleaned_records if record.source_id == "polymarket" and record.series_kind == "probability"]
            kalshi_rows = [record for record in cleaned_records if record.source_id == "kalshi" and record.series_kind == "probability"]
            benchmark_rows = [record for record in cleaned_records if record.source_category == "professional_benchmark"]
            qualitative_rows = [record for record in cleaned_records if record.series_kind == "qualitative"]
            write_csv(self.paths["cleaned"] / "prediction_market_probabilities_long.csv", CSV_COLUMNS, [r.as_row() for r in pm_rows + kalshi_rows])
            write_csv(self.paths["cleaned"] / "benchmark_long_form.csv", CSV_COLUMNS, [r.as_row() for r in benchmark_rows])
            write_csv(self.paths["cleaned"] / "qualitative_annotations_long.csv", CSV_COLUMNS, [r.as_row() for r in qualitative_rows])

        daily_rows = self.aggregate_daily(cleaned_records)
        daily_path = self.paths["daily"] / "daily_dataset.csv"
        write_csv(daily_path, DAILY_COLUMNS, daily_rows)

        snapshot_rows = self.build_snapshots(daily_rows)
        snapshot_path = self.paths["snapshots"] / "snapshot_table.csv"
        write_csv(snapshot_path, SNAPSHOT_COLUMNS, snapshot_rows)

        validation_report = self.validate_outputs(cleaned_records, daily_rows, snapshot_rows, results)
        validation_path = self.paths["reports"] / "data_quality_report.json"
        write_json(validation_path, validation_report)

        outputs = {
            "source_catalog": self.paths["catalog"] / "source_catalog.csv",
            "discovery_report": self.paths["reports"] / "discovery_report.json",
            "raw_records": raw_path,
            "cleaned": cleaned_path,
            "daily": daily_path,
            "snapshots": snapshot_path,
            "validation": validation_path,
        }
        if self.live:
            outputs.update(
                {
                    "prediction_market_probabilities_long": self.paths["cleaned"] / "prediction_market_probabilities_long.csv",
                    "benchmark_long_form": self.paths["cleaned"] / "benchmark_long_form.csv",
                    "qualitative_annotations_long": self.paths["cleaned"] / "qualitative_annotations_long.csv",
                    "source_discovery_markdown": self.paths["reports"] / "source_discovery_report.md",
                }
            )
        return outputs

    def _live_collectors(self, client: HttpClient, writer: RawArtifactWriter) -> dict[str, object]:
        all_collectors: dict[str, object] = {
            "polymarket": PolymarketCollector(self.paths, client, writer, focused=self.focused or self.source_filter == "polymarket"),
            "kalshi": KalshiCollector(self.paths, client, writer, focused=self.focused),
            "election_benchmarks": FiveThirtyEightCollector(self.paths, client, writer, focused=self.focused),
            "cme_fedwatch": CmeFedwatchCollector(self.paths, client, writer),
            "anthropic_references": AnthropicCollector(self.paths, client, writer),
        }
        if self.source_filter:
            return {self.source_filter: all_collectors[self.source_filter]}
        return all_collectors

    def _live_catalog_rows(self, results: list[CollectionResult]) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for result in results:
            access_status = "available" if result.records else ("partial" if result.raw_files else "unavailable")
            if any("credential" in issue.lower() or "auth" in issue.lower() for issue in result.issues):
                access_status = "credentialed"
            rows.append(
                {
                    "source_id": result.source_id,
                    "source_name": result.source_name,
                    "source_group": result.source_category,
                    "series_kind": "mixed" if result.records and len({record.series_kind for record in result.records}) > 1 else (result.records[0].series_kind if result.records else ""),
                    "acquisition_mode": "live",
                    "default_data_status": result.records[0].data_status if result.records else "unavailable",
                    "available": normalized_bool(bool(result.records)),
                    "access_status": access_status,
                    "input_path": "",
                    "raw_output_path": ", ".join(result.raw_files),
                    "notes": "; ".join(result.issues),
                }
            )
        return rows

    def _live_discovery_report(self, results: list[CollectionResult]) -> dict[str, object]:
        rows = self._live_catalog_rows(results)
        return {
            "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "catalog_columns": [
                "source_id",
                "source_name",
                "source_group",
                "series_kind",
                "acquisition_mode",
                "default_data_status",
                "available",
                "access_status",
                "input_path",
                "raw_output_path",
                "notes",
            ],
            "sources": rows,
            "summary": {
                "available_sources": sum(1 for row in rows if row["available"] == "TRUE"),
                "unavailable_sources": sum(1 for row in rows if row["available"] == "FALSE"),
            },
        }

    def _write_discovery_markdown(self, discovery_report: dict[str, object]) -> None:
        path = self.paths["reports"] / "source_discovery_report.md"
        lines = ["# Source Discovery Report", ""]
        for row in discovery_report.get("sources", []):
            lines.append(
                f"- {row['source_id']}: available={row['available']} access_status={row.get('access_status', '')} raw={row['raw_output_path']}"
            )
            if row.get("notes"):
                lines.append(f"  - notes: {row['notes']}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def discover_sources(self) -> tuple[list[dict[str, object]], dict[str, object]]:
        rows: list[dict[str, object]] = []
        now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        for source in self.sources:
            availability = self._locate_source_input(source)
            rows.append(
                {
                    "source_id": source.source_id,
                    "source_name": source.source_name,
                    "source_group": source.source_group,
                    "series_kind": source.series_kind,
                    "acquisition_mode": source.acquisition_mode,
                    "default_data_status": source.default_data_status,
                    "available": normalized_bool(availability is not None),
                    "input_path": str(availability) if availability else "",
                    "raw_output_path": str(self.paths["raw"] / f"{source.source_id}_raw.csv"),
                    "notes": source.notes,
                }
            )

        report = {
            "generated_at": now,
            "catalog_columns": [
                "source_id",
                "source_name",
                "source_group",
                "series_kind",
                "acquisition_mode",
                "default_data_status",
                "available",
                "input_path",
                "raw_output_path",
                "notes",
            ],
            "sources": rows,
            "summary": {
                "available_sources": sum(1 for row in rows if row["available"] == "TRUE"),
                "unavailable_sources": sum(1 for row in rows if row["available"] == "FALSE"),
            },
        }
        return rows, report

    def ingest_raw_data(self) -> list[Record]:
        records: list[Record] = []
        for source in self.sources:
            input_path = self._locate_source_input(source)
            if input_path is None:
                unavailable = [self._unavailable_record(source)]
                self._write_source_raw_file(source, unavailable)
                records.extend(unavailable)
                continue

            loaded_rows = self._load_rows(input_path)
            if not loaded_rows:
                unavailable = [self._unavailable_record(source, notes="Input file contained no rows.")]
                self._write_source_raw_file(source, unavailable)
                records.extend(unavailable)
                continue

            source_records: list[Record] = []
            for idx, row in enumerate(loaded_rows, start=1):
                parsed = self._row_to_record(source, row, input_path, idx)
                if parsed is not None:
                    source_records.append(parsed)
            self._write_source_raw_file(source, source_records)
            records.extend(source_records)

        return records

    def clean_records(self, raw_records: Iterable[Record]) -> list[Record]:
        cleaned: list[Record] = []
        for record in raw_records:
            if not truthy(record.is_real_data):
                continue
            if record.data_status in {"unavailable", "paywalled"}:
                continue
            if not record.raw_file_path and not record.source_url:
                continue
            cleaned.append(record)
        return cleaned

    def aggregate_daily(self, cleaned_records: Iterable[Record]) -> list[dict[str, object]]:
        buckets: dict[tuple[str, str, str, str, str], list[Record]] = defaultdict(list)
        for record in cleaned_records:
            key = (
                record.source_id,
                record.event_id,
                record.observation_date,
                record.series_kind,
                record.category,
            )
            buckets[key].append(record)

        daily_rows: list[dict[str, object]] = []
        for (source_id, event_id, observation_date, series_kind, category), rows in sorted(buckets.items()):
            first = rows[0]
            numeric_values = [
                self._safe_float(row.probability_value if row.series_kind == "probability" else row.benchmark_value)
                for row in rows
            ]
            numeric_values = [value for value in numeric_values if value is not None]
            if first.series_kind == "probability":
                daily_value = mean(numeric_values) if numeric_values else ""
                daily_value_type = "probability"
            elif first.series_kind == "benchmark":
                daily_value = mean(numeric_values) if numeric_values else ""
                daily_value_type = "benchmark"
            else:
                daily_value = ""
                daily_value_type = "qualitative"

            daily_rows.append(
                {
                    "source_id": source_id,
                    "source_name": first.source_name,
                    "source_category": first.source_category,
                    "source_group": first.source_group,
                    "platform": first.platform,
                    "series_kind": series_kind,
                    "benchmark_type": first.benchmark_type,
                    "event_id": event_id,
                    "event_name": first.event_name,
                    "category": category,
                    "observation_date": observation_date,
                    "event_date": first.event_date,
                    "daily_value": daily_value,
                    "daily_value_type": daily_value_type,
                    "unit": first.unit,
                    "use_in_probability_analysis": first.use_in_probability_analysis,
                    "data_status": self._combine_status(rows),
                    "is_real_data": normalized_bool(all(truthy(row.is_real_data) for row in rows)),
                    "source_endpoint": first.source_endpoint,
                    "source_url": first.source_url,
                    "raw_file_path": first.raw_file_path,
                    "record_count": len(rows),
                    "provenance_file": first.provenance_file,
                    "notes": "; ".join(sorted({row.notes for row in rows if row.notes})),
                }
            )
        return daily_rows

    def build_snapshots(self, daily_rows: Iterable[dict[str, object]]) -> list[dict[str, object]]:
        latest_by_key: dict[tuple[str, str], dict[str, object]] = {}
        for row in daily_rows:
            key = (str(row["source_id"]), str(row["event_id"]))
            existing = latest_by_key.get(key)
            if existing is None or str(row["observation_date"]) >= str(existing["latest_observation_date"]):
                latest_by_key[key] = {
                    "source_id": row["source_id"],
                    "source_name": row["source_name"],
                    "source_category": row["source_category"],
                    "event_id": row["event_id"],
                    "event_name": row["event_name"],
                    "category": row["category"],
                    "latest_observation_date": row["observation_date"],
                    "latest_daily_value": row["daily_value"],
                    "data_status": row["data_status"],
                    "latest_data_status": row["data_status"],
                    "source_endpoint": row["source_endpoint"],
                    "source_url": row["source_url"],
                    "raw_file_path": row["raw_file_path"],
                    "latest_provenance_file": row["provenance_file"],
                    "latest_notes": row["notes"],
                }
        return [latest_by_key[key] for key in sorted(latest_by_key)]

    def validate_outputs(
        self,
        cleaned_records: Iterable[Record],
        daily_rows: Iterable[dict[str, object]],
        snapshot_rows: Iterable[dict[str, object]],
        live_results: list[CollectionResult] | None = None,
    ) -> dict[str, object]:
        cleaned_records = list(cleaned_records)
        daily_rows = list(daily_rows)
        snapshot_rows = list(snapshot_rows)
        violations: list[str] = []

        for record in cleaned_records:
            if record.use_in_probability_analysis == "TRUE" and record.series_kind != "probability":
                violations.append(
                    f"{record.source_id}:{record.event_id} marked for probability analysis but is not a probability series."
                )
            if record.series_kind == "probability":
                value = self._safe_float(record.probability_value)
                if value is None:
                    violations.append(f"{record.source_id}:{record.event_id} is missing a probability_value.")
                elif not 0.0 <= value <= 1.0:
                    violations.append(f"{record.source_id}:{record.event_id} probability out of bounds: {value}.")
            if not record.raw_file_path and not record.source_url:
                violations.append(f"{record.source_id}:{record.event_id} has neither raw_file_path nor source_url.")

        seen_keys: set[tuple[str, str, str]] = set()
        duplicate_rows = 0
        for row in daily_rows:
            key = (
                str(row["source_id"]),
                str(row["event_id"]),
                str(row["observation_date"]),
            )
            if key in seen_keys:
                duplicate_rows += 1
            seen_keys.add(key)

        report = {
            "generated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
            "summary": {
                "cleaned_rows": len(cleaned_records),
                "daily_rows": len(daily_rows),
                "snapshot_rows": len(snapshot_rows),
                "duplicate_daily_rows": duplicate_rows,
                "violations": len(violations),
            },
            "violations": violations,
            "sources": [
                {
                    "source_id": result.source_id,
                    "normalized_rows": len(result.records),
                    "raw_files": len(result.raw_files),
                    "issues": result.issues,
                }
                for result in (live_results or [])
            ],
        }
        return report

    def _prepare_directories(self) -> None:
        for path in self.paths.values():
            ensure_dir(path)
        self._write_templates()

    def _write_templates(self) -> None:
        templates_dir = ensure_dir(self.paths["templates"])
        probability_template = templates_dir / "manual_probability_template.csv"
        benchmark_template = templates_dir / "manual_benchmark_template.csv"
        if not probability_template.exists():
            write_csv(
                probability_template,
                CSV_COLUMNS,
                [
                    Record(
                        source_id="example_probability_source",
                        source_name="Example Probability Source",
                        source_category="prediction_market",
                        source_group="prediction_market",
                        platform="manual",
                        series_kind="probability",
                        benchmark_type="manual_example",
                        event_id="example_event",
                        event_name="Example event",
                        category="example",
                        observation_date=date.today().isoformat(),
                        event_date=date.today().isoformat(),
                        raw_value="0.50",
                        probability_value="0.50",
                        benchmark_value="",
                        unit="probability",
                        use_in_probability_analysis="TRUE",
                        data_status="manual_annotation",
                        is_real_data="FALSE",
                        source_endpoint="",
                        source_url="",
                        raw_file_path="",
                        provenance_file="templates/manual_probability_template.csv",
                        provenance_row="1",
                        notes="Template example row; exclude unless marked as real data.",
                    ).as_row()
                ],
            )
        if not benchmark_template.exists():
            write_csv(
                benchmark_template,
                CSV_COLUMNS,
                [
                    Record(
                        source_id="example_benchmark_source",
                        source_name="Example Benchmark Source",
                        source_category="benchmark",
                        source_group="benchmark",
                        platform="manual",
                        series_kind="benchmark",
                        benchmark_type="manual_example",
                        event_id="example_benchmark_event",
                        event_name="Example benchmark event",
                        category="benchmark",
                        observation_date=date.today().isoformat(),
                        event_date=date.today().isoformat(),
                        raw_value="42",
                        probability_value="",
                        benchmark_value="42",
                        unit="index",
                        use_in_probability_analysis="FALSE",
                        data_status="manual_annotation",
                        is_real_data="FALSE",
                        source_endpoint="",
                        source_url="",
                        raw_file_path="",
                        provenance_file="templates/manual_benchmark_template.csv",
                        provenance_row="1",
                        notes="Template example row; exclude unless marked as real data.",
                    ).as_row()
                ],
            )

    def _write_source_raw_file(self, source: SourceSpec, rows: list[Record]) -> None:
        raw_file = self.paths["raw"] / f"{source.source_id}_raw.csv"
        write_csv(raw_file, CSV_COLUMNS, [record.as_row() for record in rows])

    def _locate_source_input(self, source: SourceSpec) -> Path | None:
        for relative in source.input_files:
            candidate = self.paths["root"] / relative
            if candidate.exists():
                return candidate
        return None

    def _load_rows(self, path: Path) -> list[dict[str, str]]:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            return read_csv_rows(path)
        if suffix == ".json":
            payload = read_json(path)
            if isinstance(payload, list):
                return [dict(row) for row in payload]
            if isinstance(payload, dict) and "rows" in payload and isinstance(payload["rows"], list):
                return [dict(row) for row in payload["rows"]]
            return []
        return []

    def _unavailable_record(self, source: SourceSpec, notes: str = "") -> Record:
        return Record(
            source_id=source.source_id,
            source_name=source.source_name,
            source_category=source.source_group,
            source_group=source.source_group,
            platform=source.source_name,
            series_kind=source.series_kind,
            benchmark_type="manual_annotation" if source.source_group != "prediction_market" else "market_probability",
            event_id=f"{source.source_id}_unavailable",
            event_name=source.source_name,
            category=source.source_group,
            observation_date=date.today().isoformat(),
            event_date="",
            raw_value="",
            probability_value="",
            benchmark_value="",
            unit="",
            use_in_probability_analysis="TRUE" if source.is_probability_series else "FALSE",
            data_status="unavailable",
            is_real_data="FALSE",
            source_endpoint="",
            source_url="",
            raw_file_path="",
            provenance_file="",
            provenance_row="",
            notes=notes or source.notes or "Source unavailable in this workspace.",
        )

    def _row_to_record(
        self,
        source: SourceSpec,
        row: dict[str, str],
        input_path: Path,
        row_number: int,
    ) -> Record | None:
        is_real = truthy(row.get("is_real_data", True))
        data_status = str(row.get("data_status") or source.default_data_status).strip() or source.default_data_status
        if not is_real and data_status == source.default_data_status:
            data_status = "manual_annotation"
        observation_date = self._coerce_row_date(row.get("observation_date")) or date.today().isoformat()
        event_date = self._coerce_row_date(row.get("event_date")) or ""
        event_id = str(row.get("event_id") or f"{source.source_id}_{row_number}").strip()
        event_name = str(row.get("event_name") or row.get("name") or event_id).strip()
        category = str(row.get("category") or source.source_group).strip()
        raw_value = str(row.get("raw_value") or row.get("value") or "").strip()
        notes = str(row.get("notes") or "").strip()

        if source.is_probability_series:
            probability_value = self._extract_probability_value(row)
            benchmark_value = ""
            unit = str(row.get("unit") or "probability").strip()
            use_in_probability_analysis = "TRUE"
        elif source.series_kind == "benchmark":
            probability_value = ""
            benchmark_value = self._extract_benchmark_value(row)
            unit = str(row.get("unit") or "benchmark").strip()
            use_in_probability_analysis = "FALSE"
        else:
            probability_value = ""
            benchmark_value = ""
            unit = str(row.get("unit") or "text").strip()
            use_in_probability_analysis = "FALSE"

        if data_status == "paywalled" and not notes:
            notes = "Source is paywalled and not ingested."
        if data_status == "partial" and not notes:
            notes = "Partial source availability detected."

        return Record(
            source_id=source.source_id,
            source_name=source.source_name,
            source_category=source.source_group,
            source_group=source.source_group,
            platform=source.source_name,
            series_kind=source.series_kind,
            benchmark_type="market_probability" if source.is_probability_series else "benchmark_reference",
            event_id=event_id,
            event_name=event_name,
            category=category,
            observation_date=observation_date,
            event_date=event_date,
            raw_value=raw_value,
            probability_value=probability_value,
            benchmark_value=benchmark_value,
            unit=unit,
            use_in_probability_analysis=use_in_probability_analysis,
            data_status=data_status,
            is_real_data="TRUE",
            source_endpoint=str(input_path),
            source_url=str(input_path),
            raw_file_path=str(input_path),
            provenance_file=str(input_path.relative_to(self.paths["root"])),
            provenance_row=str(row_number),
            notes=notes,
        )

    def _extract_probability_value(self, row: dict[str, str]) -> str:
        for key in ("probability_value", "probability", "value", "raw_value"):
            value = row.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    def _extract_benchmark_value(self, row: dict[str, str]) -> str:
        for key in ("benchmark_value", "value", "raw_value"):
            value = row.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    def _coerce_row_date(self, value: object) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        return coerce_date(text)

    def _safe_float(self, value: object) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _combine_status(self, rows: list[Record]) -> str:
        statuses = {row.data_status for row in rows if row.data_status}
        if not statuses:
            return ""
        if len(statuses) == 1:
            return next(iter(statuses))
        priority = [
            "unavailable",
            "paywalled",
            "partial",
            "manual_annotation",
            "manual_real_data",
            "real_downloaded_data",
            "real_api_data",
        ]
        for status in priority:
            if status in statuses:
                return status
        return sorted(statuses)[0]
