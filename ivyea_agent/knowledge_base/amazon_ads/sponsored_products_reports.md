# Sponsored Products Report Semantics And Limits

Source type: official summary
Updated: 2026-07-06

Official sources:
- https://advertising.amazon.com/help/GBYSPTSLR337JMLH
- https://advertising.amazon.com/help/G3HEFZYWZF84NPS9
- https://advertising.amazon.com/help/G89VFUTQUWFFN2VU
- https://advertising.amazon.com/help/GPDYPV4AAYCAJFKP

## Search term report

- The report contains customer searches and, for non-search or off-Amazon contexts, an inferred best-matching term.
- It includes search terms with at least one ad click. Its impressions therefore do not have to equal Campaign Manager impressions.
- ASIN-like values can represent product-detail-page delivery from automatic or product targeting; they are not necessarily literal customer text queries.
- The current public help page documents summary/daily time units and a 65-day lookback. Treat this as a dated capability and recheck the live help page before automation.

## Targeting report

- The targeting report evaluates managed targets that received at least one impression.
- A target is the configured keyword/product/category logic; a search term is the observed or inferred delivery context. Do not merge them as one field.

## Placement report

- Current Sponsored Products placement groups include top of search, rest of search, product pages, and Amazon Business where applicable.
- The public help page currently identifies the placement report as the place for off-Amazon performance metrics.
- The current public help page documents summary/daily units and a 90-day lookback. Recheck before building scheduled extraction.

## Reliability controls

- Downloadable reports can change when invalid clicks are removed; canceled purchases are excluded under the general reports help definition.
- Save the account time zone and report-generation timestamp.
- Do not infer “lost impressions” by subtracting a click-filtered search-term report from Campaign Manager totals.
- Do not interpret an ASIN-like search-term row as a keyword without checking targeting type and placement.
