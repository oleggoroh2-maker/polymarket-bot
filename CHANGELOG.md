# Changelog

## v0.9 — AI Explain

- Добавлен новый модуль `explain_engine.py`.
- AI Opportunity и STRONG-алерты теперь показывают причины сигнала.
- Объяснение использует уже рассчитанные метрики: Score, AI Quality, AI Risk, ML, ликвидность, momentum и изменения цены.
- Добавлены краткий вывод AI и уровень уверенности.
- Логика расчёта Score, фильтры, ML и частота уведомлений не изменены.
- Добавлены настройки `AI_EXPLAIN_ENABLED` и `AI_EXPLAIN_MAX_FACTORS`.
