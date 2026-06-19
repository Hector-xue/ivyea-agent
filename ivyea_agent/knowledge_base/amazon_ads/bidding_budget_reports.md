# Sponsored Products Bidding, Budgets, And Reports

Source type: official summary
Updated: 2026-06

Sources:
- https://advertising.amazon.com/solutions/products/sponsored-products
- https://advertising.amazon.com/library/guides/getting-started-with-sponsored-ads
- https://advertising.amazon.com/library/guides/sponsored-products-best-practices
- https://advertising.amazon.com/library/guides/targeting-with-sponsored-products

## Core Facts

- Sponsored Products campaigns have no monthly or upfront fees; advertisers pay when shoppers click.
- A bid is the maximum amount the advertiser is willing to pay for a click.
- Daily budget is the amount the advertiser is willing to spend per day over a calendar month. Budgets are not paced evenly throughout the day, so small budgets can spend quickly if demand is high.
- Sponsored Products only appear when advertised items are in stock.
- Amazon Ads reports for Sponsored Products include search term, targeting, advertised product, placement, performance over time, and purchased product reports.
- Search term reports help identify top-converting search terms.
- Targeting reports show keyword/target performance.
- Placement reports show performance by placement.
- Performance over time reports show CPC and spend changes over time.
- Amazon suggests using dynamic bidding up and down to maximize performance, or dynamic down only when optimizing toward ROAS.
- Amazon suggests testing match types for 1-2 weeks before pausing keywords.
- When using multiple match types for one keyword, exact can carry higher bids, phrase lower, and broad lowest, depending on goals and performance.

## Ivyea Agent Rules

- Budget expansion should be recommended only when spend is constrained and ACOS/CVR are healthy relative to target.
- A campaign that spends early but has weak conversion should not receive budget increases; diagnose targeting, placement, listing, price, reviews, and offer first.
- Bid changes should be incremental. Default single-step bid changes should stay within 20% unless the user overrides.
- For high ACOS terms with orders, prefer bid reduction or placement/budget control before negation.
- For no-order terms with sufficient clicks and spend, consider negative exact/phrase if relevance is weak; otherwise send to listing/offer diagnosis.
- For winner terms, recommend harvesting into exact/phrase and checking budget limits.
- Use ACOS relative to target, not ACOS alone. A term can be healthy if it is below target and contributes orders.
- If a campaign is limited by budget and contains both winners and waste, split winners before raising budget.

## Report-to-Action Mapping

- Search term report: discover winners, waste, irrelevant traffic, ASIN targets, and listing keyword gaps.
- Targeting report: adjust bids for keywords/targets already under management.
- Placement report: evaluate top-of-search/product-page placement effects.
- Advertised product report: identify ASINs consuming spend or converting well.
- Purchased product report: detect cross-ASIN purchase behavior and new targeting opportunities.

## Risk Controls

- Do not execute bid or budget changes without preview, evidence, and rollback path.
- Treat recent changes with a cooling period before recommending repeated bid adjustments.
- Separate launch, ranking-defense, profit, and clearance objectives before judging performance.
