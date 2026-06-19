# Amazon Budget Pacing

Use this skill when the user asks about campaign budget, bid changes, scaling,
daily pacing, or ACoS/ROAS control.

## Workflow

1. Read target ACoS, margin, lifecycle stage, campaign objective, and current
   budget/bid constraints.
2. Separate scaling candidates from waste control candidates.
3. Prioritize budget increases only where relevance, orders, and conversion
   signal are healthy.
4. Keep bid changes small and auditable. Respect cooldown windows.
5. Check placement and report cadence before recommending structural changes.

## Guardrails

- Single bid decrease should stay within the configured limit.
- Do not scale because spend is low; scale because the traffic is relevant and
  conversion signal supports it.
- For launch-stage ASINs, preserve learning budget for relevant terms.
- For mature ASINs, bias toward efficiency and waste isolation.

## Output

Group recommendations into: raise budget, hold budget, reduce bid, monitor, and
needs listing/price/review fix before budget changes.
