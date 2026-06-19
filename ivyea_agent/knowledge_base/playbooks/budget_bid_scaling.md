# Budget And Bid Scaling Playbook

Source type: official_plus_community_consensus
Updated: 2026-06

Official sources:
- https://advertising.amazon.com/library/guides/targeting-with-sponsored-products
- https://advertising.amazon.com/library/guides/sponsored-products-best-practices
- https://advertising.amazon.com/solutions/products/sponsored-products

Community basis:
- Operator discussions commonly recommend isolating winners before scaling budget and using small bid changes with attribution cooldown. Treat as field practice.

## Official Grounding

- Sponsored Products are CPC ads; advertisers pay when shoppers click.
- Advertisers control targeting, spend, bids, and daily/lifetime budgets.
- Daily budgets are not necessarily paced evenly throughout the day.
- Amazon suggests dynamic bidding up and down to maximize performance, and dynamic down only when optimizing toward ROAS.
- Amazon recommends setting exact match bids higher than phrase, and phrase higher than broad when using all three match types for the same keyword.
- Reports should be reviewed regularly and benchmarked against campaign goals.

## Bid Rules

- Use target ACOS or margin-derived target ACOS as the benchmark.
- High ACOS with orders: reduce bid gradually instead of negating.
- Healthy ACOS with repeat orders: consider bid increase if impression share or placement is constrained.
- Broad discovery terms should usually bid lower than phrase/exact harvest terms.
- Default single bid step should be small, normally within 10-20%.
- Do not repeatedly change bid before enough post-change data arrives.

## Budget Rules

- Do not raise budget on mixed campaigns until waste is controlled or winners are isolated.
- Raise budget when:
  - campaign spends out or appears constrained;
  - ACOS is at or below target;
  - orders are repeatable;
  - query quality is understood.
- Hold or reduce budget when:
  - campaign has high spend with no orders;
  - winner and waste traffic are mixed and cannot be separately controlled;
  - listing/offer conversion appears weak.

## Scaling Sequence

1. Identify winner terms and campaigns.
2. Remove or suppress obvious waste.
3. Harvest winners into manual phrase/exact where needed.
4. Check whether budget is limiting healthy traffic.
5. Increase budget or bid in small steps.
6. Recheck after attribution/cooldown window.

## Ivyea Guardrails

- Every budget increase must include evidence of healthy ACOS/CVR and a note about mixed traffic risk.
- Every bid change must include current bid, new bid, percent change, reason, and rollback path.
- If current bid is unavailable, output advisory only; do not execute.
- If target ACOS is unknown, infer conservatively from margin if available; otherwise ask or use configured default.
