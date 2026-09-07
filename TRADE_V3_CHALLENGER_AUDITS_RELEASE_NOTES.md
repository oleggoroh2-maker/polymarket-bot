# Trade v3 + Challenger + Audits

- Adds Trade Intelligence v3 in Shadow/Paper mode only.
- v3 evaluates entry quality using Final v2, EV, continuation, Entry Quality, Chase Risk, News v2, regime and purchased-side payoff.
- Adds frozen Paper Challenger for `v2 SKIP -> v3 TRADE` disagreements.
- Adds `Trade v3 Audit`: v3 outcomes, Challenger outcomes, `v2 TRADE -> v3 SKIP` counterfactuals and Final v2 tier performance.
- Adds `News v2 Audit`: catalyst class, outcome support and priced-in-risk performance.
- Existing live routing, Quality v3, EV/Risk gate, Trade v2 and Telegram TRADE decisions are unchanged.
