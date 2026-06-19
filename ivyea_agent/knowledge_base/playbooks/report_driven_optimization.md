# Report-driven optimization cadence

## Required evidence

Advertising actions should be tied to a report source. A single search-term row
is not enough for every decision; combine reports when the action affects budget,
placement, or campaign structure.

## Report roles

- Search term report: find converting customer queries, wasted queries, and terms
  that should become exact/phrase targets.
- Targeting report: judge whether a keyword, product target, or match type is
  working after aggregation.
- Placement report: identify whether top-of-search, rest-of-search, or product
  page placement is driving the outcome.
- Performance-over-time report: check CPC, spend, and conversion trend before
  declaring a recent change successful or failed.
- Purchased product report: discover cross-ASIN purchase behavior and possible
  targeting/listing opportunities.

## Weekly operating cadence

- Daily: check budget exhaustion, obvious spend spikes, and write failures.
- Twice weekly: review high-click zero-order terms, high ACoS ordered terms, and
  winners that need harvesting.
- Weekly: compare placement performance and campaign budget allocation.
- Monthly: review campaign architecture, duplicated keywords, stale tests, and
  listing/ad keyword alignment.

## Decision guardrail

When a recommendation lacks its report source, mark it as `[推断]` and ask for the
missing report instead of executing. When report sources disagree, prefer the
longer window for suppression and the shorter window for anomaly alerts.

