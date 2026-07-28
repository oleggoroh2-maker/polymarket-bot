"""Compact deterministic explanations for Polymarket alerts.

The module does not alter scoring, filtering, ML predictions or alert logic.
It only formats metrics already available in the alert payload.
"""

from __future__ import annotations

import math
from typing import Any, Optional


_TIMEFRAMES = (
    ("5 минут", "change_5m"),
    ("15 минут", "change_15m"),
    ("1 час", "change_1h"),
    ("24 часа", "change_24h"),
)


def _number(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _first_number(signal: dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = _number(signal.get(key))
        if value is not None:
            return value
    return None


def _as_percent(value: Optional[float]) -> Optional[float]:
    """Accept ML probabilities in either 0..1 or 0..100 format."""
    if value is None:
        return None
    return value * 100.0 if 0.0 <= value <= 1.0 else value


def _main_price_change(signal: dict[str, Any]) -> tuple[Optional[str], Optional[float]]:
    explicit = _number(signal.get("change_percent"))
    timeframe = str(signal.get("timeframe") or "").strip()
    if explicit is not None:
        return timeframe or None, explicit

    known: list[tuple[str, float]] = []
    for label, key in _TIMEFRAMES:
        value = _number(signal.get(key))
        if value is not None:
            known.append((label, value))

    if not known:
        return None, None
    return max(known, key=lambda item: abs(item[1]))


def _momentum_direction(signal: dict[str, Any], price_change: Optional[float]) -> str:
    raw = str(signal.get("momentum") or "").strip()
    upper = raw.upper()

    if any(token in upper for token in ("PUMP", "GROWTH", "BULL", "UP")):
        return "BULLISH"
    if any(token in upper for token in ("DIP", "DROP", "BEAR", "DOWN")):
        return "BEARISH"
    if "NEUTRAL" in upper:
        return "NEUTRAL"
    if price_change is not None:
        if price_change > 0:
            return "BULLISH"
        if price_change < 0:
            return "BEARISH"
    return "NEUTRAL"


def _indicator(value: float, good_from: float, medium_from: float) -> str:
    if value >= good_from:
        return "🟢"
    if value >= medium_from:
        return "🟡"
    return "🔴"


def _signed(value: float) -> str:
    return f"{value:+.1f}%"


def _risk_text(risk: Optional[float]) -> Optional[str]:
    if risk is None:
        return None
    if risk <= 25:
        return "низкий"
    if risk <= 45:
        return "средний"
    if risk <= 65:
        return "повышенный"
    return "высокий"


def _build_conclusion(
    *,
    price_change: Optional[float],
    volume_change: Optional[float],
    liquidity_change: Optional[float],
    ml_percent: Optional[float],
    risk: Optional[float],
    direction: str,
) -> str:
    movement = abs(price_change or 0.0)
    is_rise = direction == "BULLISH"
    move_word = "рост" if is_rise else "падение"

    confirmations: list[str] = []
    if volume_change is not None and volume_change >= 50:
        confirmations.append("объёмом")
    if liquidity_change is not None and liquidity_change >= 10:
        confirmations.append("ликвидностью")
    if ml_percent is not None and ml_percent >= 65:
        confirmations.append("ML-моделью")

    if movement >= 30 and confirmations:
        joined = " и ".join(confirmations[:2])
        first = f"Сильный {move_word} подтверждается {joined}."
    elif movement >= 30:
        first = f"Зафиксирован сильный {move_word} цены."
    elif movement >= 10:
        first = f"Движение цены заметное: {move_word} набирает силу."
    else:
        first = "Сигнал выделен сочетанием текущих рыночных метрик."

    if ml_percent is not None and ml_percent < 40:
        second = "Однако ML пока слабо подтверждает продолжение движения."
    elif risk is not None and risk >= 65:
        second = "Высокий риск повышает вероятность резкого разворота."
    elif ml_percent is not None and ml_percent >= 70 and (risk is None or risk <= 45):
        second = "Вероятность продолжения движения выше средней."
    elif confirmations:
        second = "Сигнал выглядит подтверждённым, но продолжение не гарантировано."
    else:
        second = "Для большей уверенности желательно дополнительное подтверждение."

    return f"{first}\n{second}"


def build_explanation(signal: dict[str, Any], max_factors: int = 5) -> dict[str, Any]:
    """Return only real, available metrics; unavailable changes are omitted."""
    _, price_change = _main_price_change(signal)
    direction = _momentum_direction(signal, price_change)

    # Supported aliases make the formatter forward-compatible. These values
    # are shown only when another module actually supplies them.
    volume_change = _first_number(
        signal,
        "volume_change_percent",
        "volume_change",
        "volume_percent_change",
    )
    liquidity_change = _first_number(
        signal,
        "liquidity_change_percent",
        "liquidity_change",
        "liquidity_percent_change",
    )
    ml_percent = _as_percent(_number(signal.get("ml_probability")))
    risk = _number(signal.get("ai_risk"))

    factors: list[tuple[int, str]] = []

    if price_change is not None:
        icon = "🟢" if abs(price_change) >= 30 else "🟡" if abs(price_change) >= 10 else "🔴"
        action = "выросла" if price_change >= 0 else "снизилась"
        factors.append((100, f"{icon} Цена {action}: {_signed(price_change)}"))

    if volume_change is not None:
        icon = _indicator(abs(volume_change), 100, 30)
        factors.append((90, f"{icon} Объём: {_signed(volume_change)}"))

    if liquidity_change is not None:
        icon = _indicator(abs(liquidity_change), 25, 10)
        factors.append((80, f"{icon} Ликвидность: {_signed(liquidity_change)}"))

    momentum_icon = "🟢" if direction in {"BULLISH", "BEARISH"} else "🟡"
    factors.append((70, f"{momentum_icon} Momentum: {direction}"))

    if ml_percent is not None:
        icon = _indicator(ml_percent, 70, 45)
        factors.append((95, f"{icon} ML: {ml_percent:.1f}%"))

    if risk is not None and len(factors) < max_factors:
        risk_label = _risk_text(risk)
        icon = "🟢" if risk <= 25 else "🟡" if risk <= 55 else "🔴"
        factors.append((50, f"{icon} AI Risk: {risk:.0f}% ({risk_label})"))

    selected = [text for _, text in sorted(factors, key=lambda item: item[0], reverse=True)[:max_factors]]

    conclusion = _build_conclusion(
        price_change=price_change,
        volume_change=volume_change,
        liquidity_change=liquidity_change,
        ml_percent=ml_percent,
        risk=risk,
        direction=direction,
    )

    return {
        "factors": selected,
        "conclusion": conclusion,
    }


def format_ai_explain(signal: dict[str, Any], max_factors: int = 5) -> str:
    explanation = build_explanation(signal, max_factors=max_factors)
    lines = [
        "━━━━━━━━━━━━━━",
        "",
        "📌 Почему AI выбрал этот сигнал",
        "",
    ]
    lines.extend(explanation["factors"] or ["🟡 Недостаточно данных для подробного объяснения"])
    lines.extend([
        "",
        "💡 Вывод AI",
        "",
        explanation["conclusion"],
    ])
    return "\n".join(lines)
