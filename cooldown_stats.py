from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from database import get_recent_smart_cooldown_events, get_smart_cooldown_stats


_CATEGORY_LABELS = {
    "market_id": "Market ID / slug",
    "event_group": "Event / группа",
    "question": "Совпадение вопроса",
    "opportunity": "AI Opportunity",
    "other": "Другое",
}


def get_cooldown_dashboard(hours: int = 24, limit: int = 10) -> dict[str, Any]:
    events = (
        get_recent_smart_cooldown_events(hours, limit)
        if int(limit) > 0
        else []
    )
    return {
        "stats": get_smart_cooldown_stats(hours),
        "events": events,
    }


def format_cooldown_summary(stats: dict[str, Any], compact: bool = False) -> str:
    categories = stats.get("categories") or {}
    rate = stats.get("reduction_rate")
    rate_text = f"{float(rate):.1f}%" if rate is not None else "нет данных"
    if compact:
        return (
            f"🛡 Smart Cooldown ({stats.get('hours', 24)}ч)\n"
            f"🚫 Заблокировано: {int(stats.get('blocked') or 0)}\n"
            f"✅ Повторов разрешено: {int(stats.get('allowed_repeats') or 0)}\n"
            f"📉 Отсечено повторов: {rate_text}"
        )

    return (
        f"🛡 Smart Cooldown ({stats.get('hours', 24)}ч)\n\n"
        f"🚫 Заблокировано повторов: {int(stats.get('blocked') or 0)}\n"
        f"• Market ID / slug: {int(categories.get('market_id') or 0)}\n"
        f"• Event / группа: {int(categories.get('event_group') or 0)}\n"
        f"• Совпадение вопроса: {int(categories.get('question') or 0)}\n"
        f"• AI Opportunity: {int(categories.get('opportunity') or 0)}\n"
        f"• Другое: {int(categories.get('other') or 0)}\n\n"
        f"✅ Повторов разрешено: {int(stats.get('allowed_repeats') or 0)}\n"
        f"Причина: новое движение цены ≥ порога\n\n"
        f"📉 Доля заблокированных повторов: {rate_text}"
    )


def _format_remaining(value: Any) -> str:
    try:
        hours = max(0.0, float(value))
    except (TypeError, ValueError):
        return "—"
    whole = int(hours)
    minutes = int(round((hours - whole) * 60))
    if minutes == 60:
        whole += 1
        minutes = 0
    return f"{whole}ч {minutes:02d}м"


def _format_time(value: Any) -> str:
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError):
        return str(value or "—")


def format_cooldown_event(item: dict[str, Any], number: int) -> str:
    decision = str(item.get("decision") or "")
    blocked = decision == "blocked"
    icon = "❌" if blocked else "✅"
    title = str(item.get("title") or item.get("market_id") or "Без названия")
    category = _CATEGORY_LABELS.get(str(item.get("category") or "other"), "Другое")
    family = str(item.get("signal_family") or "OTHER")
    move = item.get("price_move_percent")
    move_text = "—" if move is None else f"{float(move):+.1f}%"
    lines = [
        f"{number}. {icon} {title}",
        f"Тип: {family}",
        f"Решение: {'заблокирован' if blocked else 'повтор разрешён'}",
        f"Причина: {category}",
        f"Изменение цены: {move_text}",
    ]
    if blocked:
        lines.append(f"Осталось: {_format_remaining(item.get('remaining_hours'))}")
    lines.append(f"Время: {_format_time(item.get('created_at'))}")
    return "\n".join(lines)


def format_cooldown_dashboard(dashboard: dict[str, Any]) -> str:
    stats = dashboard.get("stats") or {}
    events = dashboard.get("events") or []
    text = format_cooldown_summary(stats)
    if not events:
        return text + "\n\nПоследних решений пока нет."
    return text + "\n\n📋 Последние решения\n\n" + "\n\n".join(
        format_cooldown_event(item, number)
        for number, item in enumerate(events, start=1)
    )
