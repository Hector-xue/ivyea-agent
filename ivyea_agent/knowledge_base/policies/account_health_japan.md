# Amazon Japan Account Health evidence workflow

Source basis: the authenticated Japan Seller Central Account Health dashboard and performance notifications.

## Required evidence

- JP account/marketplace, exact Japanese notification, dashboard state, policy category, and affected ASIN/order;
- requested action, deadline, case history, and prior submissions;
- relevant invoice, supplier, product-safety, customer-service, or corrective-action evidence;
- original Japanese text plus a faithful translation when analysis is performed in another language.

## Decision boundary

- The live Japanese notice controls; translated summaries and global API state events are supporting context only.
- Diagnose the specific policy/metric rather than treating Account Health as one score.

## Guardrails

- Never promise reinstatement or provide invented response times.
- Do not submit altered documents or conceal linked-account facts.
