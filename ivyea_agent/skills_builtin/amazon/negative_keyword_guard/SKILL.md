# Amazon Negative Keyword Guard

Use this skill before approving, explaining, or revising negative keyword actions.

## Workflow

1. Identify the proposed negative target and match type.
2. Check whether the term is protected by profile, user memory, brand/core word,
   competitor strategy, launch exploration, or data sufficiency.
3. Confirm whether the problem is irrelevant traffic, listing conversion,
   price/review weakness, or mixed signal.
4. Downgrade risky phrase negatives to exact negatives when the query is isolated.
5. If evidence is weak, recommend observation or bid control instead of negative.

## Hard Stops

- Do not negative protected terms.
- Do not negative broad roots that can match relevant long-tail traffic.
- Do not negative strategic core words solely because short-window ACoS is high.
- Do not treat one weak 7-day window as proof when 30/60-day history is missing.

## Output

For every negative recommendation, show: term, match type, evidence, risk level,
blocked/proceed decision, and safer alternative if blocked.
