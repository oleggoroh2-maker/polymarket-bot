# Paper Trading v1

- Does not place real orders and does not change Quality v3 / EV-Risk filtering.
- Opens one virtual $100 trade per unique AI signal after the first successful Telegram delivery.
- Uses existing 1h / 6h / 24h AI Memory outcomes to calculate mark-to-market PnL.
- Reports PnL, ROI, win rate, profit factor and 24h category breakdown.
- Applies configurable 1.0% synthetic trading friction to the displayed realistic PnL.
- Adds the Telegram button `💼 Paper Trading`.

Config defaults:
- `PAPER_TRADING_MODE = True`
- `PAPER_TRADING_BANK_USD = 10000.0`
- `PAPER_TRADE_STAKE_USD = 100.0`
- `PAPER_TRADING_COST_PERCENT = 1.0`
