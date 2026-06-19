# Search Term Lifecycle Playbook

Source type: official_plus_community_consensus
Updated: 2026-06

Official sources:
- https://advertising.amazon.com/library/guides/targeting-with-sponsored-products
- https://advertising.amazon.com/library/guides/sponsored-products-best-practices

Community basis:
- Public seller/operator discussions commonly describe this as auto/broad discovery -> manual harvest -> isolation/scale. Treat this as field practice, not Amazon policy.

## Official Grounding

- Automatic targeting can help discover shopping queries and products relevant to the advertised item.
- Manual keyword targeting gives target-level control.
- Search term report data can inform which keywords or products to add to manual campaigns.
- Manual keyword match types move from broad exposure to phrase balance to exact precision.
- Amazon recommends testing match types before pausing keywords.

## Lifecycle Stages

1. Discover
   - Source: automatic campaigns, broad match, loose match, category/product targeting.
   - Goal: collect query/product evidence, not immediate profit perfection.
   - Ivyea action: classify traffic and monitor spend, clicks, orders, CTR, CVR, ACOS.

2. Validate
   - Source: terms with clicks and enough data to judge intent.
   - Goal: decide if the query is relevant and commercially viable.
   - Ivyea action: separate irrelevant traffic from relevant-but-not-converting traffic.

3. Harvest
   - Source: search terms with repeat orders or healthy ACOS.
   - Goal: move winners into manual phrase/exact campaigns for clearer bid control.
   - Ivyea action: propose exact/phrase keyword creation and keep original discovery source controlled.

4. Scale
   - Source: harvested terms with healthy ACOS, stable CVR, and no obvious budget bottleneck.
   - Goal: increase profitable traffic without mixing it with waste.
   - Ivyea action: raise bid/budget incrementally and watch placement/ASIN concentration.

5. Suppress
   - Source: irrelevant or consistently poor terms with enough evidence.
   - Goal: stop waste while avoiding damage to future long-tail traffic.
   - Ivyea action: use negative exact first when intent is narrow; use negative phrase only when the phrase is safely irrelevant.

## Ivyea Decision Rules

- Do not treat discovery campaigns as mature profit campaigns unless the user says so.
- A winner term should be harvested before the whole discovery campaign budget is scaled.
- If a term wins in auto/broad but is absent from listing copy, create a listing feedback item.
- If a term has high clicks and no orders, first classify cause:
  - irrelevant query -> negative candidate;
  - relevant but weak conversion -> listing/offer diagnosis;
  - strategic core term -> hold or bid-control, not automatic negative.
- Keep a cooldown after bid or budget changes before recommending another adjustment.

## Data Signals

- Discovery: impressions/clicks rising, mixed query quality.
- Validate: 12-20+ clicks or meaningful spend depending on CPC/category.
- Harvest: repeat orders or ACOS below target.
- Scale: healthy ACOS and stable CVR after harvest.
- Suppress: enough clicks/spend with no orders and weak relevance.
