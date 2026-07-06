# Listings Items issue diagnostics

Source basis: Amazon Selling Partner API, **Listings Items API Issues Troubleshooting**. This is an official-document summary. It is strongest for Listings Items API/feed integrations; a Seller Central UI error must be matched by its exact message before applying the same diagnosis.

## Official facts

- Listing submissions use the JSON Schema supplied by the Product Type Definitions API.
- A submission can report a synchronous issue that prevents acceptance, while downstream catalog processing can produce asynchronous issues later. An integration must inspect both paths.
- Issue `90220` means a required attribute identified in the message was not supplied. Validate against the current Product Type Definitions schema for the target marketplace and product type.
- Issue `4000001` means the value supplied for the named attribute is invalid. Some attributes accept only a constrained set of values, so validate the value against the current schema rather than guessing a replacement.
- Amazon recommends using Product Type Definitions to identify required attributes and Notifications to track definition changes.

## Diagnostic intake

Collect before recommending a fix:

1. exact issue code and complete message;
2. marketplace, seller ID scope, SKU, ASIN if one exists, product type, and submission timestamp;
3. the submitted attributes after secret redaction;
4. current Product Type Definitions schema/version and validation output;
5. whether the issue came from the synchronous response, later listing retrieval, feed processing report, or Seller Central UI;
6. recent catalog or product-type metadata changes.

## Safe resolution workflow

1. Reproduce schema validation locally with the same marketplace and product type.
2. For a missing attribute, supply the attribute named by the issue only after confirming its schema requirements and units.
3. For an invalid value, use the schema's allowed values, type, format, and conditional requirements.
4. Resubmit the smallest corrected payload and then inspect asynchronous issues; submission acceptance alone is not proof that the listing is fully healthy.
5. If the exact issue is not covered by the official guide, preserve the payload and processing result and escalate with a reproducible case.

## Guardrails

- Do not invent an attribute or enumeration from another marketplace/category.
- Do not map an arbitrary Seller Central UI banner to an API issue code without matching evidence.
- Do not publish credentials, access tokens, personal data, or unredacted identity documents in a diagnostic record.
