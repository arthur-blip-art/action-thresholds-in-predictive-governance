# Data Dictionary and Reproducibility Notes

## Fed dec-to-Apr
- `polymarket_fed_decisions_dec_to_apr_long.csv`: cleaned long-form transaction-implied probability series for the four exact FOMC decision markets.
- `fed_dec_to_apr_daily.csv`: daily last/mean/min/max probability aggregation by meeting, outcome, and token.
- `fed_dec_to_apr_snapshots.csv`: closest-observation snapshot table for T-30, T-14, T-7, T-3, T-1, final_available.
- `fed_realized_decisions_dec_to_apr.csv`: official Fed decision comparator table.
- `nyfed_effr_dec_to_apr.csv`: realized-rate context from the NY Fed reference-rates page.

## Anthropic
- `polymarket_anthropic_price_history_long.csv`: cleaned price-history series for retained valuation-threshold markets.
- `anthropic_valuation_daily.csv`: daily last/mean/min/max probability aggregation by threshold.
- `anthropic_valuation_snapshots.csv`: snapshot table from first_available through final_available.
- `anthropic_threshold_mapping.csv`: market_slug/token_id/threshold mapping used for the thesis figures.

## Column Notes
- `source_endpoint_type = prices_history`: standardized Polymarket CLOB history endpoint.
- `source_endpoint_type = trades_fallback`: transaction-level reconstruction from Polymarket trade data because prices history was unavailable or unusable.
- `interpolated_or_inferred`: not used in the primary thesis tables; if a horizon is missing, it is omitted rather than inferred.

## Interpretation Flags
- Standardized price-history data: direct `prices_history` endpoints.
- Trade-level fallback reconstruction: trade prices aggregated into an implied probability series.
- Interpolated values: not used in final thesis tables.
- Missing/omitted horizons: snapshot horizons with no qualifying observation are excluded from the output.
