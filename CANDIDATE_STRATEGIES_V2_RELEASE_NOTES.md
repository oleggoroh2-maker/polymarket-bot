# Candidate Strategies v2 + Confidence Recalibration

Shadow-only update.

- AI Simulator stores candidate scores at signal creation time, before the 24h outcome exists.
- Candidate strategies: Frozen Recalibration; Recalibration + No OPPORTUNITY; Recalibration + Combination bonus; Recalibration + Combination bonus + No OPPORTUNITY.
- Combination bonus requires at least 100 historical samples and is capped.
- Confidence Recalibration stores a future-only calibrated confidence based on historical raw-confidence buckets with shrinkage.
- No live alert filters, live Score, or delivery rules are changed.
