# Amazon Marketing Cloud analysis and privacy boundaries

Source basis: Amazon Ads official Amazon Marketing Cloud product page. Dataset availability, lookback, paid features, APIs, and activation paths vary by account.

## Product boundary

- Amazon Marketing Cloud (AMC) is a cloud-based, privacy-safe clean-room solution for analytics and audience building over pseudonymized signals.
- AMC can combine eligible Amazon Ads signals with advertiser and supported third-party inputs inside the governed environment.
- Outputs are aggregated and anonymous; row-level user export or re-identification is outside the product contract.
- AMC analysis, reporting APIs, audience activation, and signal onboarding are distinct workflows with separate permissions and validation needs.

## Analysis contract

Record:

- instance/advertiser, marketplace or region, dataset/schema version, event-time field, lookback and query execution time;
- join keys and match rates, filters, conversion definition, attribution logic, aggregation threshold, null handling, and deduplication;
- SQL/query version, input-table coverage, output metric definitions, and activation destination when applicable.

## Guardrails

- Do not interpret unmatched records as people who did not see or buy; they may reflect scope, identity, consent, or ingestion limits.
- Do not attempt to export row-level identities or combine outputs to re-identify users.
- An AMC path analysis is observational unless the design supplies a valid causal control or experiment.
