# Amazon Ads negative keywords and negative product targeting

Source type: official
Source URL: https://advertising.amazon.com/help/GTEHPEG5BXY9UX5W
Retrieved at: 2026-07-01
License: amazon_public_docs_summary
Quality: authoritative
Applies to: Sponsored ads campaigns where advertisers add negative keywords or negative products to prevent ads from showing on selected traffic.
Does not apply to: account-specific exceptions, marketplace-specific pages not cited below, or future Amazon changes after the retrieved date.

## What the official source establishes

- Amazon Ads provides negative keywords and/or negative product targeting controls to help prevent ads from serving for selected search terms or products.
- Negative targeting is a traffic exclusion mechanism; it is not a diagnosis of why a relevant term failed to convert.
- Negative keyword match types can differ from positive match behavior and must be selected carefully.
- Negative product targeting can exclude ASINs, categories, or product targets depending on campaign/ad product support.
- The official feature is intended to improve relevance and reduce wasted spend, but it can also block future discovery if overused.

## Operational interpretation for Amazon sellers

- Use negative exact when a specific shopper query is clearly irrelevant or repeatedly wasteful.
- Use negative phrase only when the phrase itself is irrelevant and blocking related long-tail traffic is acceptable.
- For low-conversion but semantically relevant terms, first check listing conversion, price, reviews, coupon, and inventory before negating.
- In launch workflows, prefer observation, bid control, or isolation before suppressing relevant discovery traffic.

## Evidence required before action

- Search term report with sufficient clicks/spend and no or poor orders relative to target ACoS and unit economics.
- Semantic classification: brand, competitor, ASIN string, core category, attribute, scenario, irrelevant, uncertain.
- Protected-term list from user profile, ASIN strategy, brand strategy, and category core words.
- Conversion root cause check: listing, price, reviews, coupon, inventory, offer eligibility.

## Guardrails

- Do not negative brand terms, protected core words, strategic competitor terms, or launch discovery terms solely because of short-term ACoS.
- Do not use phrase negatives on broad roots unless the root is clearly irrelevant.
- Do not convert community thresholds into hard rules without account validation.
- Every negative recommendation must include evidence and a reversible fallback where possible.

## Related Ivyea workflows

- amazon.negative_keyword_guard
- amazon.launch_playbook
- run_patrol
- propose_actions

## Source URLs

- https://advertising.amazon.com/help/GTEHPEG5BXY9UX5W
