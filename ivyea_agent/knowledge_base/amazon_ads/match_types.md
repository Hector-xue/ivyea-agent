# Amazon Ads keyword match types

Source type: official
Source URL: https://advertising.amazon.com/help/GHTRFDZRJPW6764R
Retrieved at: 2026-07-01
License: amazon_public_docs_summary
Quality: authoritative
Applies to: Sponsored ads keyword targeting where broad, phrase, and exact match behavior determines query reach.
Does not apply to: account-specific exceptions, marketplace-specific pages not cited below, or future Amazon changes after the retrieved date.

## What the official source establishes

- Amazon Ads defines keyword match types as controls for how shopper search terms can match advertiser keywords.
- Broad match is the widest keyword reach and may include related variations, synonyms, or terms in any order depending on the ad product and marketplace behavior.
- Phrase match is narrower than broad and is intended to match the keyword phrase or close variations while allowing additional words before or after.
- Exact match is the narrowest keyword match and is intended to match the exact keyword or close variations.
- Match type selection affects reach, relevance, spend concentration, and the amount of search term data generated.

## Operational interpretation for Amazon sellers

- Use broad or automatic targeting for launch discovery when budget is available and irrelevant traffic is monitored.
- Graduate validated search terms into phrase/exact manual campaigns when they show orders, strong CTR/CVR, or strategic relevance.
- Exact match is the preferred control layer for proven winners and for isolating single-query performance.
- Phrase and broad require stronger negative keyword governance because one keyword can map to many shopper queries.

## Evidence required before action

- Search term report rows with impressions, clicks, spend, orders, sales, CTR, CVR, CPC, ACoS/ROAS.
- Launch or maturity stage of the ASIN and whether the term is protected discovery traffic.
- Listing readiness signals: image, title, bullets, price, coupon, review count, rating, inventory.

## Guardrails

- Do not judge a keyword only by campaign-level ACoS when query-level data is mixed.
- Do not suppress protected broad discovery terms in launch stage without checking listing, review, price, and inventory readiness.
- When in doubt, isolate with exact targeting or exact negatives rather than broad root suppression.

## Related Ivyea workflows

- amazon.launch_playbook
- amazon.negative_keyword_guard
- run_patrol
- run_account_diagnosis

## Source URLs

- https://advertising.amazon.com/help/GHTRFDZRJPW6764R
