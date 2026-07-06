# Amazon display ads and Sponsored Display migration boundary

Source basis: Amazon Ads official display advertising product pages. Product names and console workflows are actively evolving.

## Product boundary

- Amazon's current display offering spans self-service display workflows and programmatic display through Amazon DSP.
- The self-service product formerly called Sponsored Display is being integrated into the broader display-ads offering; existing campaign labels and API/report schemas may retain legacy names.
- Display placements can extend beyond the Amazon store to Amazon properties and eligible third-party apps or websites.
- Billing, optimization, audience, creative, placement, and eligibility options vary by workflow and marketplace.

## Migration evidence

- account, marketplace, console product label, campaign type and API entity/version;
- legacy Sponsored Display identifier mapping to current display campaign identifiers;
- objective, optimization strategy, billing model, audience/targeting, inventory source, creative, and destination;
- report attribution window, view-through treatment, reach/frequency, and sales scope.

## Guardrails

- Do not assume every legacy Sponsored Display field has a one-to-one replacement.
- Do not mix self-service display and DSP performance without normalizing buying model, supply, fees, attribution, and measurement.
- Preserve legacy identifiers and raw exports during migration so historical reporting remains auditable.
