# Amazon seller tax reports and liability boundaries

Source basis: Amazon SP-API Tax Reports documentation. Tax rules, report availability, and seller obligations vary by jurisdiction and change over time.

## Official facts

- Amazon exposes different tax reports by region, including US sales-tax, European VAT, and India GST reports.
- Report availability, request versus schedule behavior, roles, and restricted-data requirements differ by report type.
- Some tax reports contain restricted data and require the applicable SP-API role and Restricted Data Token.
- Report fields can distinguish marketplace-collected tax, seller tax registrations, transaction type, jurisdiction, and shipment origin/destination.

## Evidence required for analysis

- legal seller entity, marketplace, tax-registration jurisdiction, filing period, currency, fulfillment path, and ship-from/ship-to facts;
- exact report type, generation timestamp, report row identifiers, order/refund identifiers, and transaction type;
- the current Seller Central tax document or authorized SP-API export;
- any tax authority notice or professional-adviser position supplied by the seller.

## Guardrails

- A report is transaction evidence, not a legal conclusion about registration, nexus, VAT establishment, filing, or payment liability.
- Do not assume that tax collected or withheld by Amazon eliminates every seller obligation.
- Do not merge US sales tax, EU/UK VAT, India GST, or other regimes into a single rule.
- For a filing or registration decision, identify the uncertainty and route the evidence to a qualified tax professional.
