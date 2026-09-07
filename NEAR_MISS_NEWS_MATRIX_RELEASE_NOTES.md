# Near Miss + News Catalyst Matrix

Shadow/Paper analytics update. No live routing, Quality v3, Trade v2, EV/Risk, or Telegram delivery gate is changed.

## Trade Intelligence v3 Near Miss

- Freezes `trade_v3_near_miss_score` (0–100) and `trade_v3_distance_to_trade` at signal time.
- Freezes blocker count and continuation probability for future-only diagnostics.
- Trade v3 decision logic itself is unchanged: Near Miss never turns SKIP into TRADE.
- Trade v3 Audit now shows 3h/24h counterfactual performance by Near Miss band and the closest recent SKIP candidates with their blockers.
- Alert detail shows Near Miss, gap, blocker count, and continuation for v3.

## News v2 Catalyst × Priced-in

- News v2 Audit now cross-tabulates catalyst class with LOW/MID/HIGH priced-in risk.
- Shows future-only 3h and 24h $100 counterfactual ROI for each observed combination.
- This separates a fresh useful catalyst from news that may already be reflected in price.

Legacy rows are not backfilled. The new Near Miss fields populate only for signals created after this release.
