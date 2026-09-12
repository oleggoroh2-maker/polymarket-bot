# Stable Zone Shadow Strategy v1

Future-only Shadow/Paper validation of the first Entry Feature Walk-Forward zone that stayed positive across Discovery, Validation and Holdout at 24h:

- Entry YES price: 20–50¢
- Early Score: 60–69
- Primary horizon: 24h

## What is recorded

Zone membership is frozen at the moment a new EARLY candidate is created after this release. Historical candidates are not backfilled into the future-only sample.

The report tracks 3h / 6h / 12h / 24h:

- n
- average ROI
- Profit Factor
- Win rate
- median ROI
- sequential max drawdown for $100-per-trade shadow entries

It also tracks the average ROI of the most recent 20 matured 24h trades.

## Pre-registered promotion gate

The zone is labeled `CANDIDATE` only when the future-only 24h sample has:

- n >= 40
- ROI > 0
- PF > 1.20
- last-20 ROI > -1.0%

These thresholds are fixed before the future outcomes mature.

## Safety

Shadow/Paper only. No changes to Entry Discovery v1/v2, Trade v2/v3, Quality, Routing, Cooldown, EV/Risk, or live alerts.
