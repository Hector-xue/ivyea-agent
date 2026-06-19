# Amazon Search Term Optimizer

Use this skill when the user asks to optimize Sponsored Products search terms,
keywords, ACOS, wasted spend, harvest winners, or negative targeting.

## Workflow

1. Load account context first: ASIN, site, lifecycle stage, target ACoS, margin,
   protected terms, and any remembered rejects.
2. Run account diagnosis for broad questions; run patrol for ASIN/store-level
   execution candidates.
3. Classify each term as brand, competitor, ASIN, core category, attribute,
   scene, irrelevant, or uncertain.
4. Separate traffic problems from listing conversion problems. Do not turn low
   CVR into negative keywords without checking relevance.
5. Propose actions in this order: listing/CTR/CVR feedback, long-tail harvest,
   bid adjustment, negative candidate.
6. Put executable actions into the action queue and keep blocked actions visible
   with the guardrail reason.

## Decision Rules

- Use CPA, orders, clicks, spend, and relevance together; never use ACoS alone.
- High-click zero-order terms become candidates only after the configured click
  threshold and after checking semantic relevance.
- Do not negative brand, competitor, strategic core terms, launch discovery terms,
  or terms with weak data.
- Prefer exact negatives for isolated irrelevant queries. Avoid phrase negatives
  unless the phrase is clearly irrelevant and low-risk.
- For winners, harvest into controlled manual exact/phrase targets before
  aggressive scaling.

## Output

Return an evidence-first summary with these sections: account context, winners,
waste candidates, listing feedback, queued actions, blocked actions, next check.
