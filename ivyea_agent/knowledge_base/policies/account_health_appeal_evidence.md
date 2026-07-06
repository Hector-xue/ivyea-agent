# Account Health, status changes, and appeal evidence

Source basis: Amazon's public seller-policy guide, Seller Central Account Health/notifications, and SP-API notification type documentation. Account-specific instructions always override this generic summary.

## Official facts and boundaries

- Amazon's public policy guide describes Account Health Rating as a snapshot of deactivation risk from policy compliance. It also describes order-performance metrics such as Order Defect Rate and Late Shipment Rate; current targets and consequences must be verified in the seller's Account Health page.
- SP-API documents `ACCOUNT_STATUS_CHANGED` transitions among `NORMAL`, `AT_RISK`, and `DEACTIVATED` for a subscribed seller/marketplace pair. A status event identifies a state change; it does not by itself identify the root cause or prove what appeal evidence is required.
- The Account Health dashboard, performance notification, violation-specific next steps, appeal history, and Amazon case response are the primary evidence for an affected account.

## Structured appeal intake

Capture separately:

1. marketplace, status, notification ID/date, policy or metric named by Amazon;
2. exact "why this happened" and "how to reactivate/address" text;
3. affected ASIN/order/campaign identifiers and date range;
4. every document or explanation explicitly requested;
5. prior submissions, Amazon responses, and current appeal button/status;
6. account data that supports or contradicts the stated issue.

## Reasoning contract

- Diagnose the stated enforcement, not a generic "Section 3" label.
- Separate root-cause evidence, immediate correction, and prevention/control evidence.
- If Amazon says an action is an error-review path, provide evidence responsive to that path; do not invent an admission.
- If the exact notification is missing, ask for it. A generic plan of action is not a reliable substitute.

## Guardrails

- Never fabricate invoices, letters of authorization, compliance reports, identities, or supplier relationships.
- Never promise reactivation or a response date.
- Do not recommend repeated resubmission of unchanged material.
- Do not expose buyer data, identity documents, bank details, tokens, or unredacted account identifiers.
