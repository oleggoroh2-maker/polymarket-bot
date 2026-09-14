# Feature Intel v2 button fix

Fixes a Python import-name collision in `bot.py`.

The Entry Feature Intelligence v2 functions were imported first, but later the legacy `feature_intelligence` module imported functions with the same names and overwrote them. As a result, `🧪 Feature Intel v2` displayed the legacy `📊 Feature Intelligence · Shadow Mode` report.

The Entry v2 imports now use unique aliases and the button handler calls those aliases explicitly.

No scoring, Entry Discovery, Trade v2/v3, routing, or live alert logic is changed.
