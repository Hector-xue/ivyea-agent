# Amazon professional knowledge and evidence standard

This card governs how Ivyea Agent retrieves, interprets, and cites Amazon knowledge. It is an internal control, not an Amazon policy.

## Evidence levels

1. **Official current rule or schema**: Amazon policy/help pages, API documentation, Product Type Definitions, and official release notes. Preserve marketplace, locale, category, program, and retrieval date.
2. **Official training or announcement**: Seller University, Selling Partner Blog, and Amazon Ads newsroom. Useful for workflows and feature announcements; do not silently promote marketing guidance into a mandatory policy.
3. **Account-local evidence**: the seller's notices, case log, reports, error payloads, catalog schema, and performance data. This evidence has the highest diagnostic relevance for that account, but does not become a universal Amazon rule.
4. **Official-anchored synthesis**: an Ivyea operating playbook derived from official definitions plus reversible operating practice. Label recommendations as synthesis.
5. **Community or operator hypothesis**: a lead for investigation only. It must not override official documentation or be stated as a disclosed ranking algorithm.

## Retrieval and answer contract

- Retrieve knowledge for registration, identity verification, listing errors, account health, policy, fees, restricted products, FBA, advertising, and ranking/traffic questions.
- Cite each material official claim with the supplied `[K#]` key. The final source list must contain only sources actually cited in the answer.
- For a high-risk question with no current matching source, state the evidence gap instead of producing a confident rule.
- Ask for the exact error code/message, marketplace, category/product type, affected SKU/ASIN, and timestamp when those fields can change the diagnosis.
- State whether a conclusion is an **official fact**, **account-data inference**, or **operator hypothesis**.

## Marketplace and time boundaries

- Never assume a US rule applies globally. Carry marketplace and locale metadata with evidence.
- Prefer current Product Type Definitions or authenticated Seller Central content over a static generic summary where requirements are dynamic.
- A monitored web change enters a review queue. It does not automatically overwrite a production knowledge card.
- Preserve the old snapshot, new snapshot, content hash, check time, and source URL so an update is auditable and reversible.

## Algorithm claims

Amazon does not publicly disclose every ranking, traffic allocation, auction, or enforcement signal. Publicly documented behavior may be stated as official. Patterns inferred from account data must include the observed data window and alternative explanations. Community language such as "weight," "traffic pool," or "algorithm penalty" remains a hypothesis unless a current official source explicitly supports it.

## Safety boundaries

- Do not advise policy evasion, review manipulation, fabricated documents, hidden incentivization, or bypassing identity checks.
- Do not convert a single seller case into a universal threshold.
- For potentially irreversible actions, require current official evidence plus account-local confirmation.
