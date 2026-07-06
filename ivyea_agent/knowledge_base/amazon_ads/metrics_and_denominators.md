# Amazon Ads Metrics, Denominators, And Sales Scope

Source type: official summary
Updated: 2026-07-06

Official sources:
- https://advertising.amazon.com/library/guides/basics-of-success-understanding-amazon-advertising
- https://advertising.amazon.com/library/guides/measure-improve-campaigns
- https://advertising.amazon.com/library/guides/intro-attribution-campaign-reporting

## Official facts

- Impressions count ad rendering; clicks count interactions covered by the product's reporting definition.
- CTR is clicks divided by impressions. CPC is spend divided by clicks.
- ACoS is ad spend divided by attributed sales. ROAS is attributed sales divided by ad spend.
- Metric choice must follow the campaign objective. Awareness, consideration, purchase, and loyalty goals do not use an identical success test.
- Sales scope can differ between ad products. A metric label alone is not enough to establish which promoted or brand purchases are included.

## Calculation contract

- Store raw impressions, clicks, spend, attributed orders, attributed sales, and attributed units before calculating ratios.
- Return null, not zero or infinity, when a denominator is zero or missing.
- Keep currency, account time zone, date range, time unit, ad product, report type, attribution definition, and sales scope with every metric set.
- Compare ACoS or ROAS only when currency, attribution definition, sales scope, and reporting maturity are compatible.
- Attributed sales are not automatically incremental sales, total sales, profit, or proof of organic-rank impact.

## Diagnostic order

1. Confirm the business objective and denominator.
2. Confirm the report, date range, time zone, currency, ad product, and sales scope.
3. Check whether invalid traffic, cancellations, or attribution lag can still restate the report.
4. Separate a measured account observation from an explanation of why it happened.

## Prohibited shortcuts

- Do not call ACoS “profit” without fees, cost of goods, returns, taxes, and other costs.
- Do not compare Sponsored Products and Sponsored Brands attributed sales as if their sales scope were necessarily identical.
- Do not invent missing conversions from clicks, or missing sales from orders and an assumed average order value.
