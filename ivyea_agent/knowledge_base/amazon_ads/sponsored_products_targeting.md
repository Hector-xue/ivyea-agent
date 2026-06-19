# Sponsored Products Targeting And Campaign Structure

Source type: official summary
Updated: 2026-06

Sources:
- https://advertising.amazon.com/library/guides/targeting-with-sponsored-products
- https://advertising.amazon.com/library/guides/getting-started-with-sponsored-ads
- https://advertising.amazon.com/solutions/products/sponsored-products

## Core Facts

- Sponsored Products are CPC ads for individual listings. Ads can appear in shopping results and on product detail pages, and clicks take shoppers to the advertised product detail page.
- Sponsored Products targeting has three main modes: automatic targeting, manual targeting, and negative targeting.
- Automatic targeting can be used for discovery because Amazon matches ads to relevant shopping queries and products based on product information and prior shopping queries.
- Automatic targeting groups include close match, loose match, substitutes, and complements. These groups can be bid separately.
- Manual keyword targeting supports broad, phrase, and exact match.
- Broad match gives wider exposure and may match related terms, synonyms, variations, or queries where the keyword itself is not present.
- Phrase match is more restrictive than broad and generally keeps keyword components in the same order.
- Exact match is the most restrictive and generally the most precise.
- Negative targeting excludes keywords, products, or brands that should not be associated with the ad. It can be used in automatic and manual campaigns.
- Amazon recommends evaluating a keyword after at least 20 clicks before deciding to add it as a negative target.
- Negative keywords support phrase and exact match. Negative phrase blocks queries containing the phrase or close variations. Negative exact blocks the exact phrase or close variation.
- Campaign targeting type cannot be changed after launch; changing targeting strategy requires setting up a new campaign.
- A practical structure is to separate product groups, targeting methods, and business objectives so budgets, bids, and KPIs stay interpretable.

## Ivyea Agent Rules

- Treat automatic campaigns as discovery sources. Search terms with conversions can graduate into manual phrase/exact campaigns.
- Do not recommend negative targeting from very low sample sizes unless the term is clearly irrelevant.
- Use exact/phrase negatives conservatively. Avoid broad semantic negation because it can block future relevant long-tail traffic.
- Do not auto-negate brand, competitor, ASIN, or core category terms without explicit user approval.
- For broad terms with mixed intent, prefer bid control, campaign segmentation, or listing diagnosis before negation.
- When a winning term appears in auto/broad, recommend harvesting it into a manual campaign and optionally isolating traffic with negatives only after the migration plan is clear.
- If one ASIN or variation receives almost all impressions in a mixed campaign, recommend segmentation by ASIN/pack/size before raising budget.

## Diagnostic Prompts

- Is this term relevant to the product and offer?
- Does the term have enough clicks to judge?
- Is bad performance caused by irrelevant traffic, weak listing conversion, price/review disadvantage, or mixed intent?
- Should this term be negated, bid-managed, harvested, or sent to listing optimization?
