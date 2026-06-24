# Fed Dec-to-Apr Realized Analysis

## Current Data Status
- Exact Fed decision markets covered in the audit: 2025-12, 2026-01, 2026-03, 2026-04.
- Meetings with usable local Polymarket probability rows: 2025-12, 2026-01, 2026-03, 2026-04.
- Realized Fed decision rows written: 4.
- NY Fed realized-rate rows written: 25.

This section compares Polymarket’s ex ante probabilities before FOMC decisions with the realized Federal Reserve policy outcomes. The objective is to assess whether prediction-market probabilities moved toward the realized decision early enough to become decision-relevant.

Cette section compare les probabilités ex ante issues de Polymarket avant les décisions du FOMC avec les décisions réellement prises par la Fed. L’objectif est d’évaluer si les probabilités de marché convergent vers l’issue réalisée suffisamment tôt pour devenir actionnables.

## Which Meetings Were Included
- December 2025, January 2026, March 2026, and April 2026 are included as exact meeting-specific Fed decision markets.
- February is not forced into the analysis because the regular FOMC meeting cadence does not place a decision there in this window.

## Which Meetings Had Usable Polymarket Histories
- All four meetings now have usable local probability rows in the repository.
- All recovered series are trade-based fallback histories rather than standardized prices-history files.
- December 2025, March 2026, and April 2026 were recovered from the exact Polymarket markets and can now be graphed alongside January 2026.

## Realized Fed Outcomes
- 2025-12: Lowered the target range by 25 basis points. Target range moved from 3.75-4.00 to 3.50-3.75.
- 2026-01: Maintained the target range. Target range moved from 3.50-3.75 to 3.50-3.75.
- 2026-03: Maintained the target range. Target range moved from 3.50-3.75 to 3.50-3.75.
- 2026-04: Maintained the target range. Target range moved from 3.50-3.75 to 3.50-3.75.

## Realized Rate Context
- The NY Fed EFFR file is partial in this build and reflects the accessible reference-rate table captured from the official NY Fed page.
- It should be read as realized policy-rate context, not as a forecast or a probability series.

## What Can and Cannot Be Concluded
- Polymarket probabilities can now be compared to the realized outcomes for December 2025, January 2026, March 2026, and April 2026.
- All recovered series are trade-based fallback histories rather than standardized prices_history files, so that methodological caveat remains central.
- The early snapshots are partially available in the usual way: if a target horizon predates the first local observation, it is omitted rather than fabricated.
- The rate chart and comparator file are still useful because they pin the realized policy outcome and the post-meeting rate environment to official sources.

## Thesis Framing
This is an ex-ante versus realized-outcome analysis. Polymarket provides the probability distribution before the FOMC decision, while the Federal Reserve statement gives the actual policy action and the realized rate environment. The substantive question is whether the market probability moved far enough, early enough, to be decision-relevant. That is the actionability problem, not whether Polymarket matched a separate forecaster.
