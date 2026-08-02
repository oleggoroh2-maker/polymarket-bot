from typing import Any, Iterable

import config

from ai_engine import record_alert
from calibration_engine import calibrate_signal
from similarity_engine import analyze_similarity
from market_group_engine import (
    get_market_group_key,
    select_group_representatives,
)
from alert_formatter import format_calibrated_alert
from database import save_alert, save_group_alert
from smart_cooldown import check_smart_cooldown, register_smart_cooldown


AUTO_OPPORTUNITY_ALERTS = getattr(config, "AUTO_OPPORTUNITY_ALERTS", True)
OPPORTUNITY_MIN_SCORE = getattr(config, "OPPORTUNITY_MIN_SCORE", 90)
OPPORTUNITY_MIN_AI_QUALITY = getattr(config, "OPPORTUNITY_MIN_AI_QUALITY", 70)
OPPORTUNITY_MAX_AI_RISK = getattr(config, "OPPORTUNITY_MAX_AI_RISK", 45)
OPPORTUNITY_MIN_LIQUIDITY = getattr(config, "OPPORTUNITY_MIN_LIQUIDITY", 100_000)
OPPORTUNITY_MAX_PRICE = getattr(config, "OPPORTUNITY_MAX_PRICE", 0.05)
OPPORTUNITY_COOLDOWN_HOURS = getattr(config, "OPPORTUNITY_COOLDOWN_HOURS", 72)
OPPORTUNITY_MAX_ALERTS_PER_SCAN = getattr(config, "OPPORTUNITY_MAX_ALERTS_PER_SCAN", 3)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _strongest_change(signal: dict[str, Any]) -> float:
    values = [
        _num(signal.get("change_5m"), 0.0),
        _num(signal.get("change_15m"), 0.0),
        _num(signal.get("change_1h"), 0.0),
        _num(signal.get("change_24h"), 0.0),
    ]
    return max(values, key=abs)


def calculate_opportunity_score(signal: dict[str, Any]) -> int:
    base_score = _num(signal.get("score"))
    ai_quality = _num(signal.get("ai_quality"))
    ai_risk = _num(signal.get("ai_risk"), 100.0)

    value = (
        base_score * 0.40
        + ai_quality * 0.35
        + max(0.0, 100.0 - ai_risk) * 0.25
    )

    liquidity = _num(signal.get("liquidity"))
    if liquidity >= 1_000_000:
        value += 5
    elif liquidity >= 500_000:
        value += 3

    price = _num(signal.get("price"))
    if 0 < price <= 0.01:
        value += 5
    elif price <= 0.03:
        value += 3

    return max(0, min(100, round(value)))


def _reasons(signal: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    score = int(_num(signal.get("score")))
    quality = int(_num(signal.get("ai_quality")))
    risk = int(_num(signal.get("ai_risk"), 100))
    liquidity = _num(signal.get("liquidity"))
    price = _num(signal.get("price"))
    change_24h = signal.get("change_24h")

    if score >= 95:
        reasons.append(f"Score {score}/100")
    if quality >= 75:
        reasons.append(f"AI Quality {quality}/100")
    if risk <= 35:
        reasons.append(f"низкий AI Risk {risk}/100")
    if liquidity >= 1_000_000:
        reasons.append("ликвидность выше $1M")
    elif liquidity >= 500_000:
        reasons.append("высокая ликвидность")
    if 0 < price <= 0.01:
        reasons.append("цена до 1¢")
    if change_24h is not None and abs(_num(change_24h)) >= 20:
        reasons.append(f"движение за 24ч {_num(change_24h):+.1f}%")

    return reasons[:4]


def is_opportunity(signal: dict[str, Any]) -> bool:
    score = int(_num(signal.get("score")))
    quality = int(_num(signal.get("ai_quality"), -1))
    risk = int(_num(signal.get("ai_risk"), 101))
    liquidity = _num(signal.get("liquidity"))
    price = _num(signal.get("price"))

    if score < OPPORTUNITY_MIN_SCORE:
        return False
    if quality < OPPORTUNITY_MIN_AI_QUALITY:
        return False
    if risk > OPPORTUNITY_MAX_AI_RISK:
        return False
    if liquidity < OPPORTUNITY_MIN_LIQUIDITY:
        return False
    if price <= 0 or price > OPPORTUNITY_MAX_PRICE:
        return False

    momentum = str(signal.get("momentum") or "")
    strongest = abs(_strongest_change(signal))

    # Сигнал должен иметь подтверждение: заметное движение,
    # сильный momentum либо исключительно высокий профиль.
    return (
        strongest >= 20
        or "PUMP" in momentum
        or "GROWTH" in momentum
        or "DIP" in momentum
        or (score >= 95 and quality >= 75)
    )


def check_opportunities(
    signals: list[dict[str, Any]],
    excluded_market_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    if not AUTO_OPPORTUNITY_ALERTS:
        return []

    excluded = {str(item) for item in excluded_market_ids}
    candidates: list[dict[str, Any]] = []

    for signal in signals:
        market_id = str(signal.get("id") or "")
        if not market_id or market_id in excluded:
            continue
        if not is_opportunity(signal):
            continue
        opportunity_score = calculate_opportunity_score(signal)
        prepared = {
            **signal,
            "alert_type": "AI_OPPORTUNITY",
            "alert_label": "⭐ AI OPPORTUNITY",
            "current_price": float(signal["price"]),
            "old_price": signal.get("price_24h"),
            "change_percent": signal.get("change_24h"),
            "volume_change_percent": signal.get("volume_change_24h"),
            "liquidity_change_percent": signal.get("liquidity_change_24h"),
            "timeframe": "24 часа",
            "absolute_move": 0.0,
            "required_move": 0.0,
            "opportunity_score": opportunity_score,
            "opportunity_reasons": _reasons(signal),
        }
        candidates.append(prepared)

    candidates.sort(
        key=lambda item: (
            -int(item.get("opportunity_score") or 0),
            -int(item.get("score") or 0),
            -float(item.get("liquidity") or 0),
        )
    )

    enriched_candidates: list[dict[str, Any]] = []
    for alert in candidates:
        alert.update(analyze_similarity(alert))
        alert.update(calibrate_signal(alert))
        enriched_candidates.append(alert)

    grouped = select_group_representatives(enriched_candidates)
    grouped.sort(
        key=lambda item: (
            -int(item.get("opportunity_score") or 0),
            -int(item.get("calibration_score") or 0),
            -int(item.get("score") or 0),
            -float(item.get("liquidity") or 0),
        )
    )
    selected = grouped[:OPPORTUNITY_MAX_ALERTS_PER_SCAN]
    result: list[dict[str, Any]] = []
    group_cooldown_hours = float(
        getattr(config, "MARKET_GROUP_COOLDOWN_HOURS", 24)
    )

    for alert in selected:
        market_id = str(alert["id"])
        group_key = get_market_group_key(alert)
        cooldown = check_smart_cooldown(alert)
        if cooldown.blocked:
            continue

        save_alert(market_id, "AI_OPPORTUNITY")
        save_group_alert(group_key, "ANY_ALERT", market_id)
        register_smart_cooldown(alert)
        try:
            alert["ai_signal_id"] = record_alert(alert)
        except Exception:
            alert["ai_signal_id"] = None
        result.append(alert)

    return result


def format_opportunity(alert: dict[str, Any]) -> str:
    return format_calibrated_alert(alert, opportunity=True)

