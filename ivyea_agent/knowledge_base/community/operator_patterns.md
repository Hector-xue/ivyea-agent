# Community Operator Patterns Intake Template

Source type: community template
Updated: 2026-06

Purpose:
This file defines how Ivyea Agent should ingest public forum/operator knowledge
from sources such as 知无不言, seller communities, agency blogs, and practitioner
posts.

## Policy

- Do not copy full forum posts, paid content, private content, or member-only content into the built-in knowledge base.
- Store only short paraphrased takeaways, source URL, author/site if public, capture date, and confidence.
- Treat forum knowledge as operational experience, not official policy.
- If community advice conflicts with Amazon official docs, official docs win unless the user explicitly chooses an account-specific override.
- Do not store personal data, screenshots with account identifiers, order/customer information, or private seller metrics from forum posts.

## Intake Schema

Each community-derived note should be converted into this shape:

```json
{
  "id": "community.<topic>.<slug>",
  "source_url": "https://...",
  "source_name": "forum/blog/community",
  "captured_at": "YYYY-MM-DD",
  "topic": "ads|listing|inventory|pricing|compliance|ops",
  "claim": "Short paraphrased operational claim.",
  "evidence_type": "anecdote|case-study|multi-operator-consensus|official-reference",
  "confidence": "low|medium|high",
  "use_when": ["conditions"],
  "do_not_use_when": ["conditions"],
  "ivyea_rule": "How the agent should use it."
}
```

## Initial Community Themes To Curate Manually

- Search term graduation: auto/broad discovery to manual exact/phrase.
- Core term protection: do not negate strategic category terms only because early ACOS is high.
- Negative keyword risk: phrase negatives can block useful long-tail variants.
- Launch period interpretation: distinguish ranking spend from mature profit spend.
- Listing/ad mismatch: high relevant clicks with weak conversion often points to offer or listing proof gap.
- Budget isolation: separate winners before scaling budgets.
- Bid cool-down: avoid repeated bid changes before attribution stabilizes.

## Confidence Rules

- Low: single anecdote, no metrics, unclear category.
- Medium: repeated operator consensus or case with clear constraints.
- High: aligns with official docs and repeated field evidence.
