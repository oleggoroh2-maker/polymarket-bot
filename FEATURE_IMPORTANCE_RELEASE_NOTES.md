# Feature Importance Engine v1.0

Changed files:
- feature_engine.py
- ai_engine.py
- bot.py
- config.py

Features:
- New `🧠 AI Insights` menu button and `/insights` command.
- Historical factor analysis at the 24-hour AI Memory checkpoint.
- Rankings for Score, AI Quality, AI Risk, ML, liquidity, price move,
  volume change, liquidity change, and Similarity when enough data exists.
- Best historical range for each factor with sample count and continuation rate.
- Category comparison.
- Similarity and calibration values are now persisted for new signals.

The engine is diagnostic only. It does not change alert filtering, calibration,
ML weights, or signal quality tiers.
