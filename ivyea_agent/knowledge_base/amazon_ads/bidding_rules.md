# Sponsored Products bidding rules and bid adjustments

Source type: official
Source URL: https://advertising.amazon.com/help/GDLQ5D2BNFCAU3TW
Retrieved at: 2026-07-01
License: amazon_public_docs_summary
Quality: authoritative
Applies to: Sponsored Products bid controls, bidding rules, and bid adjustment governance.
Does not apply to: account-specific exceptions, marketplace-specific pages not cited below, or future Amazon changes after the retrieved date.

## What the official source establishes

- Amazon Ads provides bid controls and bidding rules for Sponsored Products to help advertisers manage bids according to performance or schedule conditions.
- Bids influence auction competitiveness but do not guarantee impressions, placement, clicks, or sales.
- Placement and dynamic bidding settings can change effective bid behavior beyond the default keyword or target bid.
- Bid rules should be monitored because automated changes can compound spend if account goals or conversion conditions change.

## Operational interpretation for Amazon sellers

- Use bid changes as a bounded lever after checking CTR/CVR, listing, offer, and query relevance.
- For proven winners under target ACoS, increase bids gradually and monitor placement mix and CPC inflation.
- For relevant but inefficient terms, consider controlled bid reductions before negative targeting.
- Keep a cooldown window after bid changes so the system can gather stable post-change data.

## Evidence required before action

- Target ACoS/ROAS, margin, CPC, CVR, clicks, orders, sales, spend, placement performance.
- Current bidding strategy, placement modifiers, budget constraints, recent change history.
- Inventory and offer readiness before scaling bids.

## Guardrails

- Do not make large bid reductions on protected core terms without root-cause diagnosis.
- Do not stack bid rules and placement modifiers blindly; inspect effective bid exposure.
- Do not treat a bid increase as safe when budget, inventory, or offer constraints are binding.

## Related Ivyea workflows

- run_patrol
- propose_actions
- run_offer_audit

## Source URLs

- https://advertising.amazon.com/help/GDLQ5D2BNFCAU3TW
