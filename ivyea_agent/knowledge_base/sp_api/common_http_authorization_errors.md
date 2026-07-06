# SP-API HTTP and authorization error triage

Source basis: Amazon Selling Partner API, **Resolve Common HTTP and Authorization Error Codes**. This card applies to SP-API integrations, not ordinary Seller Central account registration.

## Official diagnostic groups

- `400 Bad Request`: validate required headers/body fields, date/time formatting, identifiers, URL encoding, content type, and the request against the current API schema. Pass pagination tokens back without modifying them.
- `403 Forbidden`: check credentials/tokens, required SP-API roles, seller versus vendor credentials, regional endpoint, resource path/version, marketplace support, and seller account status.
- `404 Not Found`: verify identifier spelling/case, marketplace scope, deletion state, and whether an asynchronously created resource is being polled too early.
- `429 Too Many Requests`: follow the operation's rate-limit behavior and use bounded retry/backoff; do not treat repeated immediate retry as a fix.

## Named authorization examples

- `MD1000`: the official guide associates this with authorizing a Draft application through the production OAuth workflow; use the documented beta authorization flow while the application is in Draft.
- `MD5101`: the OAuth redirect URI does not match a redirect URI configured for the application.
- `MD9100`: the application lacks a login URI and redirect URI.
- `SPDC8143`: the authorization attempt is made as a non-primary user; use the primary user where the official workflow requires it.

## Evidence required before action

Record the HTTP status, Amazon error code/message/request ID, operation and API version, regional endpoint, marketplace, application state, role mapping, token age, and a secret-redacted request shape. Never store or display access tokens, refresh tokens, client secrets, authorization headers, signatures, or full personal data.

## Guardrails

Do not rotate credentials, change application roles, or ask a seller to reauthorize until the exact failure path supports that action. Verify against the current official page because credential lifetimes, role mappings, endpoints, and deprecation dates can change.
