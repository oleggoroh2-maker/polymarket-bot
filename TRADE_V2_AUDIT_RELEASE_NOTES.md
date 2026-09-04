# Trade v2 Audit + SKIP Counterfactual

## Added
- New Telegram button: `🎯 Trade v2 Audit`.
- Detailed future-only audit for Trade Intelligence v2 signals.
- Shows TRADE/SKIP, YES/NO side, market regime, entry YES price, Entry Quality, Chase Risk, Final Signal, EV, Risk, frozen stake and frozen exit plan.
- Shows available real side-aware PnL at 1h / 3h / 6h / 12h / 24h.
- For TRADE decisions, shows actual PnL at the exit horizon selected at entry.
- For SKIP decisions, calculates a counterfactual fixed-$100 trade using the same side-aware execution math.
- SKIP report shows hypothetical PnL and `avoided` PnL, plus 24h results by skip reason.

## Safety / methodology
- No live alert filtering was changed.
- No historical Trade v2 decision is rewritten.
- No future checkpoint is used to choose TRADE/SKIP, stake, or exit horizon.
- Counterfactual SKIP PnL is analytics only.
