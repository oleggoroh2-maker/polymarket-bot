# Expected Value + Risk Engine v1

- Adds `ev_risk_engine.py` after Final Signal scoring.
- Estimates continuation probability, expected move and downside from current signal evidence only.
- Adds a 0–100 risk score using AI Risk, liquidity, price extremes, category and ML weakness.
- Adds a live Telegram precision gate after Quality Engine v3.
- No category is disabled. Strong signals from CRYPTO, AI/TECH, OTHER, SPORTS, POLITICS and ETF can all pass.
- AI Memory records candidates before the gate, so filtered alerts continue contributing to learning/analytics.
- Telegram alerts show EV, Risk and the gate decision reason.
