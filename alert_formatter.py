"""Unified compact Telegram formatting for calibrated Polymarket alerts.

Telegram text messages cannot reproduce the two-column card or use different font
sizes. This formatter keeps the same information hierarchy using compact blocks and
only displays metrics that are present in the real alert payload.
"""

from __future__ import annotations

from typing import Any, Optional

import config
from explain_engine import format_ai_explain


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _signed(value: float) -> str:
    return f"{value:+.1f}%"


def _direction(alert: dict[str, Any], opportunity: bool) -> str:
    alert_type = str(alert.get("alert_type") or "").upper()
    momentum = str(alert.get("momentum") or "").upper()
    change = _num(alert.get("change_percent"))

    if "DIP" in alert_type or "DIP" in momentum or "BEAR" in momentum:
        return "DIP"
    if "PUMP" in alert_type or "PUMP" in momentum or "GROWTH" in momentum:
        return "PUMP"
    if change < 0:
        return "DIP"
    if change > 0:
        return "PUMP"
    return "OPPORTUNITY" if opportunity else "NEUTRAL"


def _direction_icon(direction: str) -> str:
    return {
        "PUMP": "🔥",
        "DIP": "💧",
        "OPPORTUNITY": "⭐",
        "NEUTRAL": "⚙️",
    }.get(direction, "⚙️")


def _trend_text(direction: str) -> str:
    return {
        "PUMP": "Сильный рост",
        "DIP": "Сильное снижение",
        "OPPORTUNITY": "Возможность",
        "NEUTRAL": "Нейтральный",
    }.get(direction, "Нейтральный")


def _signal_strength(confidence: float) -> int:
    return max(1, min(5, round(confidence / 20.0)))


def _risk_label(risk: float) -> str:
    if risk <= 30:
        return "Низкий"
    if risk <= 55:
        return "Средний"
    if risk <= 75:
        return "Повышенный"
    return "Высокий"


def _alert_heading(alert: dict[str, Any], opportunity: bool) -> str:
    label = "⭐ AI OPPORTUNITY" if opportunity else str(
        alert.get("alert_label") or "📊 СИГНАЛ"
    )
    badge = str(alert.get("calibration_badge") or "🟡 WATCH")
    return f"{label}  {badge}"


def format_calibrated_alert(
    alert: dict[str, Any],
    *,
    opportunity: bool = False,
) -> str:
    """Return one consistent compact alert format for every alert type."""

    direction = _direction(alert, opportunity)
    direction_icon = _direction_icon(direction)
    stars = str(alert.get("calibration_stars") or "★☆☆☆☆")
    confidence = _num(alert.get("calibration_confidence"))
    score = int(_num(alert.get("score")))
    quality = int(_num(alert.get("ai_quality")))
    risk = int(_num(alert.get("ai_risk")))
    current_price = _num(alert.get("current_price", alert.get("price")))
    old_price = _optional_num(alert.get("old_price"))
    change = _optional_num(alert.get("change_percent"))
    volume = _optional_num(alert.get("volume"))
    volume_change = _optional_num(alert.get("volume_change_percent"))
    liquidity_change = _optional_num(alert.get("liquidity_change_percent"))
    ml = _optional_num(alert.get("ml_probability"))
    opportunity_score = int(_num(alert.get("opportunity_score")))
    strength = _signal_strength(confidence)

    lines = [
        _alert_heading(alert, opportunity),
        f"{direction_icon} {direction}  {stars} ({confidence:.0f}/100)",
        "",
        f"📊 {alert.get('title', 'Без названия')}",
        "",
    ]

    if old_price is not None:
        lines.append(
            f"💰 Цена: {old_price * 100:.2f}¢ → {current_price * 100:.2f}¢"
        )
    else:
        lines.append(f"💰 Цена: {current_price * 100:.2f}¢")

    if change is not None:
        lines.append(f"📈 Изменение: {_signed(change)}")
    timeframe = alert.get("timeframe")
    if timeframe:
        lines.append(f"⏱ Период: {timeframe}")

    liquidity = _num(alert.get("liquidity"))
    lines.append(f"💧 Ликвидность: ${liquidity:,.0f}")

    if volume is not None and volume > 0:
        volume_line = f"📊 Объём: ${volume:,.0f}"
        if volume_change is not None:
            volume_line += f" ({_signed(volume_change)})"
        lines.append(volume_line)
    elif volume_change is not None:
        lines.append(f"📊 Объём: {_signed(volume_change)}")

    if liquidity_change is not None:
        lines.append(f"🌊 Изм. ликвидности: {_signed(liquidity_change)}")

    lines.extend([
        f"⭐ Score: {score}/100",
        f"🤖 AI Quality: {quality}/100",
        f"⚠️ AI Risk: {risk}/100",
        f"🧠 ML: {ml * 100:.1f}%" if ml is not None else "🧠 ML: накопление данных",
        "",
        "━━━━━━━━━━━━━━",
        "",
        "🎯 ПРОГНОЗ AI",
        f"Направление: {direction}",
    ])

    if change is not None:
        lines.append(f"Потенциал движения: {abs(change):.1f}%")
    if opportunity and opportunity_score:
        lines.append(f"Opportunity: {opportunity_score}/100")
    lines.append(f"Сила сигнала: {strength}/5")

    lines.extend([
        "",
        "🚀 МОМЕНТУМ",
        f"Общий: {score}/100",
    ])
    if volume_change is not None:
        lines.append(f"Объём: {_signed(volume_change)}")
    lines.append(f"Тренд: {_trend_text(direction)}")

    lines.extend([
        "",
        "🛡 БЕЗОПАСНОСТЬ",
        f"AI Risk: {_risk_label(risk)} ({risk}/100)",
    ])
    if liquidity >= 1_000_000:
        lines.append("Ликвидность: Очень высокая")
    elif liquidity >= 100_000:
        lines.append("Ликвидность: Высокая")
    elif liquidity >= 10_000:
        lines.append("Ликвидность: Достаточная")
    else:
        lines.append("Ликвидность: Низкая")

    lines.extend([
        "",
        "⚖️ ОЦЕНКА РЫНКА",
        f"Калибровка: {confidence:.0f}/100",
        f"Качество: {alert.get('calibration_badge') or '🟡 WATCH'}",
    ])
    if opportunity_score:
        lines.append(f"Opportunity: {opportunity_score}/100")

    lines.extend([
        "",
        "📅 ИНФОРМАЦИЯ",
        f"Категория: {alert.get('category', '—')}",
        f"До завершения: {int(_num(alert.get('days_left')))} дней",
    ])

    samples = int(_num(alert.get("calibration_samples")))
    strong_rate = _optional_num(alert.get("calibration_strong_rate"))
    if samples and strong_rate is not None:
        lines.extend([
            "",
            "🏆 ИСТОРИЧЕСКАЯ КАЛИБРОВКА",
            f"Похожих случаев: {samples}",
            f"Сильное продолжение: {strong_rate:.1f}%",
        ])

    if getattr(config, "AI_EXPLAIN_ENABLED", True):
        lines.extend([
            "",
            format_ai_explain(
                alert,
                max_factors=int(getattr(config, "AI_EXPLAIN_MAX_FACTORS", 8)),
            ),
        ])

    url = alert.get("url")
    if url:
        lines.extend(["", f"🌐 {url}"])

    return "\n".join(lines)
