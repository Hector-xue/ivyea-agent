# Amazon US selling fees and estimate reliability

Source basis: Amazon US pricing pages and the SP-API Product Fees API documentation. Values and categories change; the current marketplace page and account transaction data are authoritative.

## Official facts

- Amazon describes standard selling fees as selling-plan fees plus referral fees. Additional costs can apply to optional programs such as FBA and Amazon Ads.
- On the cited US pricing page at retrieval time, the Individual plan is listed as USD 0.99 per item sold and the Professional plan as USD 39.99 per month. These are US values, not global defaults.
- Referral fees vary by category and can use a percentage of total price or a minimum amount; category classification and the current fee schedule matter.
- Amazon provides Manage All Inventory estimates, the Revenue Calculator, Fee Preview reports, and Product Fees API estimates.
- The Product Fees API explicitly states that returned fees are estimates and are not guaranteed; actual fees can vary.

## Evidence required for a fee diagnosis

- marketplace, transaction date, fulfillment channel/program, SKU/ASIN, category, size/weight inputs, item price, shipping/gift-wrap values, currency, and tax context;
- exact fee type/amount from the settlement or transaction report;
- current pricing/fee schedule or estimate timestamp and request inputs;
- any promotion, surcharge, storage age, returns/removals, or program enrollment affecting the charge.

## Safe calculation contract

- Label calculator/API output as an estimate.
- Label settlement/transaction records as observed account charges.
- Do not compare estimates and actual charges without aligning marketplace, date, SKU/ASIN, dimensions, fulfillment, category, and price inputs.
- Do not present tax or VAT conclusions as legal advice.
