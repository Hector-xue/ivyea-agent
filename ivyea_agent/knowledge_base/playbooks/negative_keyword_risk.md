# Negative Keyword Risk Controls

Source type: official_plus_community_consensus
Updated: 2026-06

Official sources:
- https://advertising.amazon.com/library/guides/targeting-with-sponsored-products

Community basis:
- Seller/operator discussions repeatedly warn that aggressive negative phrase rules can block useful long-tail traffic. Treat as field risk control.

## Official Grounding

- Negative targeting excludes keywords, products, or brands that should not be associated with ads.
- Negative targeting can be used in automatic and manual campaigns.
- Amazon recommends evaluating keyword performance after at least 20 clicks before making it a negative target.
- Amazon recommends negative phrase or negative exact for negative keywords.
- Negative phrase blocks queries containing the complete phrase or close variations.
- Negative exact blocks the exact phrase or close variation.
- Negative targets can be removed later, but Amazon recommends letting negative keywords run for two weeks or more before changing strategy.

## Risk Model

- Negative exact is narrower and usually safer for one poor query.
- Negative phrase is stronger and riskier because it can suppress useful variants.
- Negative product/ASIN should be used when product-page traffic is clearly non-complementary or strategically unwanted.
- A term can be poor because the traffic is wrong, or because the listing/offer fails to convert relevant traffic. These require different actions.

## Do Not Auto-Negate

- Brand defense terms.
- Competitor terms unless the account strategy says conquesting is unwanted.
- ASIN strings without product-targeting context.
- Core category terms during launch/ranking defense.
- Terms with too little data.
- Relevant terms that expose listing, price, image, review, or offer weakness.
- Terms already recently adjusted or previously rejected by the user.

## Ivyea Approval Requirements

Every negative keyword action must show:

- term/query;
- campaign/ad group if known;
- match type proposed;
- clicks, orders, spend, sales, ACOS/CVR when available;
- relevance diagnosis;
- risk note;
- rollback/removal path.

## Default Action Mapping

- Clearly irrelevant single query, sufficient clicks: negative exact.
- Irrelevant phrase family, repeated waste across variants: negative phrase, manual approval required.
- Relevant term, high ACOS with orders: reduce bid or isolate campaign, not negative.
- Relevant term, high clicks no orders: listing/offer diagnosis before negative.
- Strategic term: observe or bid-control, not negative unless user overrides.
