# Variation relationship and schema diagnostics

Source basis: Amazon Product Type Definitions API and Amazon's public product-listing guide. Current marketplace/product-type schema controls the valid attributes and variation themes.

## Official schema behavior

- Product Type Definitions can return current catalog requirements for a given marketplace and product type.
- The `parentageLevel` parameter can request schemas for `CHILD`, `PARENT`, or `NONE` listing types.
- Amazon documents that a `CHILD` schema includes attributes applicable to a variation relationship and requires `parent_sku`; a `PARENT` schema describes the variation-group container; `NONE` excludes variation attributes for standalone listings.
- The public listing guide describes variations as versions of a similar product that differ by a factor such as color or size and identifies Variation Wizard as one Seller Central workflow.

## Diagnostic intake

- marketplace, product type, requirements mode/version, parentage level, variation theme, parent SKU, child SKUs, ASINs, and exact issue code/message;
- attribute values that are common versus child-specific;
- whether the family is new, an update, or a merge of standalone listings;
- current Product Type Definitions schema for each applicable parentage level.

## Safe resolution workflow

1. Confirm the products belong in one legitimate variation family; do not use a family merely to aggregate traffic or reviews.
2. Retrieve the current schema for the exact marketplace/product type and correct parentage level.
3. Validate required parent/child attributes, allowed variation themes, value formats, and child uniqueness.
4. Submit the smallest correction and inspect both synchronous and asynchronous listing issues.

## Guardrails

- Do not create invalid parent-child relationships between materially different products.
- Do not assume a variation theme supported in one category/marketplace is valid in another.
- Do not delete/rebuild a live family without preserving identifiers, offer impact, and a rollback plan.
