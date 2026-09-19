# Pilot Engine v1

- Adds a future-only PAPER pilot for the exact Stable Zone Challenger v2 rule.
- No historical backfill: pilot launch timestamp is persisted on first run.
- Default simulated stake: $20 per accepted signal.
- Risk controls: max 5 concurrent positions, max 8 new trades/day, $100 realized drawdown kill-switch.
- Every eligible post-launch candidate is frozen as TRADE or SKIP with a reason.
- 24h pilot report shows ROI, PF, Win, Median, PnL, MaxDD, open positions and skip reasons.
- Real Polymarket order execution is deliberately NOT connected in v1.
- Existing Live/Trade/Challenger logic is unchanged.
