# Category-normalized analytics + Positive Zone Shadow v1

- Outcome Recalibration v2 and Positive Zone Finder explicitly use Category v2 title reclassification on historical rows; stored historical categories are not rewritten.
- Added Positive Zone Shadow Trader with frozen-at-entry membership for selected historical candidate zones.
- Shadow statistics are side-aware $100 counterfactuals at 3h/24h (with all checkpoints retained internally), using 1% cost.
- Current frozen zones: CRYPTO×Similarity70–79, CRYPTO×Volume300+, Score<40×Similarity70–79, CRYPTO×Liquidity100+, Price≥50¢×CRYPTO, Price20–50¢×CRYPTO.
- The existing Positive Zones button now also prints future-only shadow validation.
- No changes to live routing, Trade v2/v3, Quality, Cooldown, EV/Risk, or Telegram delivery gates.
