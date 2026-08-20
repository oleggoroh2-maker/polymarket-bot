# Final Signal Engine v1

- Added `final_signal_engine.py` with one explainable 0–100 Final Signal score.
- Uses Score calibration, AI Quality/Risk, ML, Confidence, Similarity, price,
  liquidity, liquidity/volume changes, category and confirmed combinations.
- Encodes the observed non-monotonic Score behaviour (60–74 positive, 85+ negative).
- Adds conservative AI/TECH and CRYPTO combination bonuses and penalties for
  OPPORTUNITY / 85+ with $1M+ liquidity.
- Runs before Quality Engine v3 and is visible in Telegram alerts.
- Does **not** block alerts in v1. Quality Engine v3 remains the delivery gate.
