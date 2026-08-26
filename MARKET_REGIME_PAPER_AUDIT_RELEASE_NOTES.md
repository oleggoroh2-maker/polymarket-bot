# Market Regime + Paper Audit v2

- Paper PnL is now side-aware: PUMP = buy YES, DIP = buy NO.
- Existing Paper trades are recalculated from stored entry and checkpoint YES prices; no DB reset is required.
- Added `🔎 Paper Audit` with entry/exit YES price, traded side, shares, gross PnL, costs, net PnL and AI Memory directional return.
- Added shadow Market Regime classification: NORMAL, MOMENTUM, EVENT_SHOCK, CHAOS_MANIPULATION.
- Regime is frozen at alert delivery and stored with each new Paper trade.
- Paper report now includes 24h PnL by market regime.
- Market Regime does NOT change Quality v3, EV/Risk, alert delivery, or position sizing.
