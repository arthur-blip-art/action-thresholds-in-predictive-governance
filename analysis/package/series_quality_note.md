# Series Quality Note

## Fed
- All four meetings are exact Polymarket Fed decision markets.
- The recovered series are trade-level fallback reconstructions, not standardized prices_history outputs.
- The daily and snapshot tables are empirical series, not interpolated or inferred paths.
- Missing horizons in the snapshot table are omitted, not filled.

## Anthropic
- The chart is a threshold-probability series over discrete valuation bands.
- The raw files are prices_history outputs, but the chart window is only two dates long in the current cache, so it visually resembles a short interpolation even though no interpolation is performed.
- The proper way to read it is as a sparse daily update, not a long time series.
