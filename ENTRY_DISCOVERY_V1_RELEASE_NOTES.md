# Entry Discovery Engine v1

Shadow/Paper only. Finds modest, confirmed price acceleration before legacy 30%/50% momentum thresholds and freezes candidates at entry.

- side-aware YES/NO $100 counterfactual
- 1% transaction cost
- 1h / 3h / 6h / 12h / 24h outcomes
- 6h per-market discovery cooldown
- max 12 new early candidates per scan
- A/B report against existing delivered Paper trades
- no changes to Telegram routing, Trade v2/v3, Quality, EV/Risk or Cooldown

The first version intentionally uses transparent rules: multi-horizon price alignment plus volume/liquidity confirmation, while rejecting already-stretched moves.
