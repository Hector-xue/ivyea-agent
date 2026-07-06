# Amazon India seller registration baseline

Source basis: Amazon India's public seller registration guide. Current Seller Central prompts and Indian law take precedence.

## Official baseline

- The public guide covers account creation, GST details and verification, store name, pickup address, shipping method, bank account, product listing, and store launch.
- Amazon identifies a path for sellers offering only GST-exempt products; eligibility is product- and law-specific.
- GST verification can require the current certificate displayed by the registration workflow.

## Diagnostic inputs

- entity and seller identity, GSTIN/PAN context, and whether products are claimed exempt;
- exact GST verification state, uploaded document type, and rejection message;
- pickup address, active bank account, shipping method, and listing stage;
- timestamp and language/locale of the workflow.

## Guardrails

- Do not state that a product is GST-exempt without current legal evidence.
- Do not treat Amazon registration guidance as tax advice.
- Do not recommend using another entity's GSTIN, PAN, bank account, or address.
