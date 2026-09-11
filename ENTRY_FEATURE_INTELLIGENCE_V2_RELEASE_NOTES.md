# Entry Feature Intelligence v2

Shadow/Paper-only validation layer on top of Entry Feature Recorder v1.

## Added
- Chronological 60/20/20 walk-forward validation of frozen entry features.
- Focused 1/2/3-feature interactions around acceleration, entry price, spread, bid balance, volume/liquidity build and Early score.
- Future-only Feature Score frozen at candidate creation.
- Challenger performance by score tiers (80+, 70-79, 60-69, <60) and eligible score >=70.
- New AI menu button: `🧪 Feature Intel v2`.

## Safety / live behavior
- Entry Discovery v1 thresholds unchanged.
- Entry Intelligence v2 unchanged.
- Trade v2/v3, Quality, Routing, Cooldown and EV/Risk unchanged.
- No historical candidate is backfilled into the future-only challenger.
