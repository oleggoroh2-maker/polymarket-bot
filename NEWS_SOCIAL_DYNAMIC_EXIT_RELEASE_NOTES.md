# News & Social Intelligence v1 + Dynamic Exit Engine v2

- External context is fetched only for final live alert candidates, after existing live gates.
- Google News RSS: fresh 24h news, no API key required.
- Reddit: fresh public search as a social signal.
- X recent search is optional via `X_BEARER_TOKEN`.
- Classifications: `CONFIRMED_NEWS`, `RUMOR`, `NO_CATALYST`, `CONTRADICTED`.
- News data is stored with each new Paper trade and shown in Trade v2 Audit.
- `CONTRADICTED` can make the Paper-only Trade v2 decision SKIP; it does not block Telegram delivery.
- Dynamic Exit v2 replays checkpoints sequentially with TAKE_PROFIT / STOP_LOSS / NEWS_REVERSAL / REGIME_EXIT / TIME_EXIT. It never selects the best future checkpoint.
- Existing Quality v3 and EV/Risk live gates are unchanged.
- Existing Telegram delivery remains fail-open if external sources are unavailable.
