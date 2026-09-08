# Category v2 + Outcome Recalibration v2 + Positive Zone Finder

- Fixes category substring collisions using word-boundary matching (notably `eth` inside `Elizabeth`).
- Adds Category Audit against historical AI Memory without rewriting old rows.
- Adds Outcome-Based Recalibration v2 for 3h and 24h completed AI Memory outcomes.
- Adds Positive Zone Finder for sufficiently-supported two-factor historical contexts.
- Both recalibration modules are diagnostic/shadow only and do not alter Trade v2/v3, routing, cooldown or live score weights.
- Positive zones are discovery results and explicitly require future-only validation before any live use.
