# Pilot Engine Audit v1

- Adds read-only TRADE vs `MAX_OPEN_POSITIONS` counterfactual results at 3h/6h/12h/24h.
- Adds 24h concurrency diagnostics: peak concurrent trades, current active window, and stale missing 24h outcomes.
- Fixes Pilot risk accounting to bound daily/open counts by the decision timestamp during catch-up/restarts.
- Fixes the displayed `Open` count so old trades with delayed/missing 24h outcomes are not counted as currently open forever.
- Does not change the Challenger entry rule, historical TRADE/SKIP decisions, stake, max-open limit, or Live/Trade routing.
