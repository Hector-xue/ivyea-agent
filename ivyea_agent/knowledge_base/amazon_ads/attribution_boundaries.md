# Amazon Ads Attribution Boundaries

Source type: official summary
Updated: 2026-07-06

Official sources:
- https://advertising.amazon.com/help/G3BB9TWP5KC375TJ
- https://advertising.amazon.com/library/guides/intro-attribution-campaign-reporting
- https://advertising.amazon.com/library/guides/basics-of-amazon-attribution

## Product boundary

- Amazon Ads reporting, Amazon DSP, and Amazon Attribution do not form one universal attribution contract.
- Amazon Attribution is a measurement product for eligible non-Amazon marketing channels. Its documented model must not be copied into Sponsored Ads analysis.
- The current general conversion-attribution help page documents a 14-day click/view eligibility statement for the products covered by that page, but product-specific settings and definitions still control interpretation.
- Conversion date and ad-interaction date can differ by product or report. Confirm which date owns the conversion before aligning daily data.

## Required evidence fields

Record the following before making a comparative claim:

- ad product and campaign type;
- advertiser profile/entity and marketplace;
- report type and interface/API version when available;
- report window, account time zone, and time unit;
- click-through and view-through treatment;
- attribution window and attribution model;
- promoted-product, brand, or other sales scope;
- whether the report is mature or can still be restated.

## Interpretation gate

- Missing attribution metadata does not make a report unusable, but it lowers confidence and blocks cross-report or cross-product comparison.
- “Attributed” means the conversion satisfied that report's attribution rules. It does not by itself prove the ad caused an incremental purchase.
- If the exact product/account definition is unavailable, state the ambiguity and request a report header, console tooltip, authorized help export, or current developer-portal definition.
