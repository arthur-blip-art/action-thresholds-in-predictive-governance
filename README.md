# Forecast Data Pipeline

This repository now contains a conservative Python pipeline for:

- source discovery and availability reporting
- raw ingestion from local files when present
- normalization into traceable CSVs
- daily aggregation
- snapshot generation
- validation reporting

The pipeline never fabricates unavailable values. Missing or inaccessible sources are recorded explicitly with `data_status=unavailable`.

## Run

```bash
python3 run_pipeline.py --base-dir .
```

Outputs are written to `pipeline_outputs/`.

## Input layout

Place optional real inputs under:

- `inputs/raw/polymarket.csv`
- `inputs/raw/kalshi.csv`
- `inputs/raw/manifold.csv`
- `inputs/raw/cme_fedwatch.csv`
- `inputs/raw/election_benchmarks.csv`
- `inputs/raw/anthropic_references.csv`

CSV or JSON inputs should include, where applicable:

- `event_id`
- `event_name`
- `observation_date`
- `event_date`
- `value` or `probability_value` or `benchmark_value`
- `data_status`
- `is_real_data`

Template examples are generated in `templates/`.

