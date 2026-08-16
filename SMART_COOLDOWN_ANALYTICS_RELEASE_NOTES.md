# Smart Cooldown Analytics v2.3.1

Changed files:
- smart_cooldown.py
- cooldown_stats.py (new)
- database.py
- bot.py

Features:
- 24-hour Smart Cooldown statistics in the main statistics screen.
- `/cooldown` command and `🛡 Cooldown` menu button.
- Breakdown by Market ID/slug, event/group, normalized question, and AI Opportunity.
- Count of repeats allowed after a significant price move.
- Last 10 cooldown decisions with reason, price move, time, and remaining cooldown.
- Automatic cleanup of analytics records together with existing alert cleanup.

Existing Smart Cooldown filtering rules are unchanged.
