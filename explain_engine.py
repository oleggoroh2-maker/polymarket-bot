"""Transparent local explanations for Polymarket AI signals.

The module does not change scoring, filtering, ML predictions, or alert logic.
It only turns metrics already present in a signal into concise Telegram text.
"""

from __future__ import annotations

import math
from typing import Any, Optional


_TIMEFRAMES = (
    ("5м", "change_5m"),
    ("15м", "change_15m"),
    ("1ч", "change_1h"),
    ("24ч", "change_24h"),
)


def _number(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _known_changes(signal: dict[str, Any]) -> list[tuple[str, float]]:
    result: list[tuple[str, float]] = []
    for label, key in _TIMEFRAMES:
        value = _number(signal.get(key), None)
        if value is not None:
            result.append((label, value))
    return result


def _strongest_change(signal: dict[str, Any]) -> Optional[tuple[str, float]]:
    changes = _known_changes(signal)
    if not changes:
        return None
    return max(changes, key=lambda item: abs(item[1]))


def _trend_summary(changes: list[tuple[str, float]]) -> tuple[int, int, float]:
    meaningful = [value for _, value in changes if abs(value) >= 1.0]
    if not meaningful:
        return 0, 0, 0.0
    positive = sum(1 for value in meaningful if value > 0)
    negative = sum(1 for value in meaningful if value < 0)
    consistency = max(positive, negative) / len(meaningful)
    return positive, negative, consistency


def _risk_label(risk: int) -> str:
    if risk <= 25:
        return "низкий"
    if risk <= 45:
        return "умеренный"
    if risk <= 65:
        return "повышенный"
    return "высокий"


def _confidence_label(signal: dict[str, Any]) -> str:
    quality = int(_number(signal.get("ai_quality"), 0) or 0)
    risk = int(_number(signal.get("ai_risk"), 100) or 100)
    ml_probability = _number(signal.get("ml_probability"), None)
    history_count = len(_known_changes(signal))

    confidence_points = quality - risk * 0.45 + history_count * 4
    if ml_probability is not None:
        confidence_points += (ml_probability - 0.5) * 35

    if confidence_points >= 65:
        return "высокая"
    if confidence_points >= 40:
        return "средняя"
    return "ограниченная"


def build_explanation(signal: dict[str, Any], max_factors: int = 5) -> dict[str, Any]:
    """Build a compact deterministic explanation from existing metrics."""
    factors: list[tuple[int, str]] = []
    warnings: list[tuple[int, str]] = []

    score = int(_number(signal.get("score"), 0) or 0)
    quality = int(_number(signal.get("ai_quality"), 0) or 0)
    risk = int(_number(signal.get("ai_risk"), 100) or 100)
    liquidity = float(_number(signal.get("liquidity"), 0.0) or 0.0)
    price = float(_number(signal.get("price"), 0.0) or 0.0)
    momentum = str(signal.get("momentum") or "").strip()
    changes = _known_changes(signal)
    strongest = _strongest_change(signal)
    positive, negative, consistency = _trend_summary(changes)

    if score >= 95:
        factors.append((95, f"🟢 Базовый Score очень высокий: {score}/100"))
    elif score >= 85:
        factors.append((85, f"🟢 Сильный базовый Score: {score}/100"))
    elif score >= 70:
        factors.append((65, f"🟡 Базовый Score выше среднего: {score}/100"))
    else:
        warnings.append((65, f"🟡 Базовый Score пока ограничен: {score}/100"))

    if quality >= 80:
        factors.append((92, f"🟢 AI Quality высокий: {quality}/100"))
    elif quality >= 65:
        factors.append((72, f"🟢 AI Quality подтверждает сигнал: {quality}/100"))
    elif quality:
        warnings.append((72, f"🟡 AI Quality умеренный: {quality}/100"))

    if risk <= 25:
        factors.append((90, f"🟢 AI Risk низкий: {risk}/100"))
    elif risk <= 45:
        factors.append((68, f"🟡 AI Risk умеренный: {risk}/100"))
    elif risk <= 65:
        warnings.append((82, f"🟡 AI Risk повышенный: {risk}/100"))
    else:
        warnings.append((95, f"🔴 AI Risk высокий: {risk}/100"))

    if liquidity >= 1_000_000:
        factors.append((88, f"🟢 Очень высокая ликвидность: ${liquidity:,.0f}"))
    elif liquidity >= 500_000:
        factors.append((78, f"🟢 Высокая ликвидность: ${liquidity:,.0f}"))
    elif liquidity >= 100_000:
        factors.append((52, f"🟡 Достаточная ликвидность: ${liquidity:,.0f}"))
    elif liquidity < 10_000:
        warnings.append((90, f"🔴 Низкая ликвидность: ${liquidity:,.0f}"))

    if strongest is not None:
        timeframe, change = strongest
        icon = "🟢" if change > 0 else "🔴"
        direction = "рост" if change > 0 else "падение"
        priority = min(96, 55 + int(abs(change)))
        factors.append((priority, f"{icon} Сильнейшее движение — {direction} {change:+.1f}% за {timeframe}"))

    if len(changes) >= 2 and consistency >= 0.75:
        direction = "ростом" if positive > negative else "падением"
        factors.append((84, f"🟢 Движение подтверждается {max(positive, negative)} периодами с {direction}"))
    elif len(changes) >= 2 and positive and negative:
        warnings.append((74, "🟡 Периоды дают смешанные сигналы"))
    elif len(changes) < 2:
        warnings.append((80, "🟡 Недостаточно ценовой истории для полного подтверждения"))

    momentum_upper = momentum.upper()
    if momentum and momentum not in {"—", "NONE", "UNKNOWN"}:
        if any(token in momentum_upper for token in ("PUMP", "GROWTH", "BULL")):
            factors.append((76, f"🟢 Momentum подтверждает рост: {momentum}"))
        elif any(token in momentum_upper for token in ("DIP", "DROP", "BEAR")):
            factors.append((76, f"🔴 Momentum подтверждает снижение: {momentum}"))
        elif "NEUTRAL" in momentum_upper:
            warnings.append((55, "🟡 Momentum остаётся нейтральным"))

    ml_probability = _number(signal.get("ml_probability"), None)
    if ml_probability is not None:
        percent = ml_probability * 100.0
        if percent >= 70:
            factors.append((91, f"🟢 ML подтверждение: {percent:.1f}%"))
        elif percent >= 55:
            factors.append((62, f"🟡 ML подтверждение умеренное: {percent:.1f}%"))
        elif percent < 45:
            warnings.append((86, f"🔴 ML подтверждение слабое: {percent:.1f}%"))

    if 0 < price <= 0.003:
        warnings.append((70, "🟡 Очень низкая цена усиливает процентные колебания"))

    selected: list[str] = []
    for _, text in sorted(factors, key=lambda item: item[0], reverse=True):
        if text not in selected:
            selected.append(text)
        if len(selected) >= max_factors:
            break

    warning_texts: list[str] = []
    for _, text in sorted(warnings, key=lambda item: item[0], reverse=True):
        if text not in selected and text not in warning_texts:
            warning_texts.append(text)
        if len(selected) + len(warning_texts) >= max_factors:
            break

    selected.extend(warning_texts)

    strong_positive = sum(text.startswith("🟢") for text in selected)
    strong_warning = sum(text.startswith("🔴") for text in selected)

    alert_type = str(signal.get("alert_type") or "")
    change_percent = float(_number(signal.get("change_percent"), 0.0) or 0.0)

    if strong_warning >= 2 or risk >= 70:
        conclusion = "Сигнал заметный, но риск высокий. Нужна дополнительная проверка рынка перед решением."
    elif "STRONG_DIP" in alert_type or change_percent <= -30:
        conclusion = "Зафиксировано сильное снижение. Возможен отскок, но направление ещё требует подтверждения."
    elif "STRONG_PUMP" in alert_type or change_percent >= 30:
        conclusion = "Импульс сильный и подтверждён метриками. Следует учитывать риск входа после уже состоявшегося движения."
    elif score >= 90 and quality >= 70 and risk <= 45:
        conclusion = "Рынок выделяется сочетанием высокого Score, качества и приемлемого риска."
    elif strong_positive >= 3:
        conclusion = "Несколько независимых факторов подтверждают сигнал, но это не гарантирует дальнейшее движение."
    else:
        conclusion = "Потенциал есть, однако подтверждений пока недостаточно для высокой уверенности."

    return {
        "factors": selected,
        "conclusion": conclusion,
        "confidence": _confidence_label(signal),
        "risk_label": _risk_label(risk),
    }


def format_ai_explain(signal: dict[str, Any], max_factors: int = 5) -> str:
    explanation = build_explanation(signal, max_factors=max_factors)
    lines = ["🧠 AI Explain", "", "📌 Почему сигнал выделен"]
    lines.extend(explanation["factors"] or ["🟡 Доступных факторов пока недостаточно"])
    lines.extend([
        "",
        f"🎯 Уверенность: {explanation['confidence']}",
        "",
        "💡 Вывод AI",
        explanation["conclusion"],
    ])
    return "\n".join(lines)
