# Rolling Edge Monitor + Strategy Regime Detector

Shadow-only analytics release.

- Adds rolling current-vs-previous windows over completed 24h AI Memory outcomes.
- Monitors 100 / 250 / 500 market-signal windows.
- States: ACTIVE, EMERGING, FADING, DEAD, INSUFFICIENT.
- Flags REGIME BREAK when a previously positive edge turns non-positive with a material deterioration.
- Adds broad Strategy Regime Detector for ALL / CRYPTO / CRYPTO PUMP / PUMP / DIP.
- Uses Category v2 title reclassification.
- Does not change live routing, Trade v2/v3, Quality, Cooldown, EV/Risk, or Paper positions.
