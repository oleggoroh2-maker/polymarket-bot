# Zone Validation Suite v1

- Adds chronological 60/20/20 walk-forward validation for historical Positive Zones.
- Discovery finds zones only in the first 60%; Validation and Holdout are never used to discover them.
- STABLE requires positive adjusted outcome in both Validation and Holdout with at least 15 samples each.
- Adds decomposition of the two main historical zones (CRYPTO × Similarity 70–79 and CRYPTO × Volume Δ 300+) by Direction, Price, Volume/Liquidity change, AI Risk and Score.
- Category is reclassified through Category v2 from title; historical DB rows are not rewritten.
- Analytics/shadow only. Live routing, Trade v2/v3, Quality, Cooldown and EV/Risk are unchanged.
