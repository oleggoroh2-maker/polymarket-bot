# Trade Intelligence v2 + Exit Engine v1

- Paper/Shadow only: Telegram live delivery is unchanged.
- Adds frozen-at-entry `TRADE` / `SKIP` decision.
- Strong EVENT_SHOCK, excessive chase risk, low entry quality, extreme risk and poor payoff price can be skipped in the v2 paper strategy.
- Scores the actually purchased YES/NO side price.
- Dynamic paper sizing remains $25/$50/$75/$100/$150; SKIP uses $0.
- Adds entry-time exit horizon: 1h / 3h / 6h / 12h depending on regime, chase and risk.
- AI outcome checkpoints now include 3h and 12h for future signals.
- Paper report compares Fixed, legacy Risk v1, Trade v2 and Exit Engine v1.
- Adds v2 diagnostics by position size, PUMP/DIP side, Entry Quality and Chase Risk.
- Existing historical trades are not backfilled as v2, preventing look-ahead contamination.
