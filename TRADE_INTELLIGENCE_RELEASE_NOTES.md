# Trade Intelligence / Risk Position Engine v1

- Adds Entry Quality (0–100) and Chase Risk (0–100).
- Uses Final Signal, EV/Risk, Similarity, ML, liquidity, price/flow structure and Market Regime.
- Suggests paper-only position sizes: $25 / $50 / $75 / $100 / $150.
- EVENT_SHOCK, high chase risk and high risk automatically cap paper position size.
- Telegram delivery gates are unchanged.
- Paper Trading now compares fixed $100 against Risk Engine sizing on the same future signals.
- Existing trades remain in fixed-strategy history; Risk Engine comparison starts prospectively with v1 trades only.
