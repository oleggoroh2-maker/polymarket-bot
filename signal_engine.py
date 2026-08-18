from typing import Any, Optional

import config

from ai_engine import enrich_signal, record_alert
from calibration_engine import calibrate_signal
from similarity_engine import analyze_similarity
from market_group_engine import (
    get_market_group_key,
    select_group_representatives,
)
from alert_formatter import format_calibrated_alert
from database import save_alert, save_group_alert
from quality_live_analytics import record_quality_decision
from quality_live_v2_shadow import record_shadow_decision
from quality_engine_v3 import evaluate_quality_v3
from smart_cooldown import check_smart_cooldown, register_smart_cooldown
from confidence_engine import enrich_with_confidence
from price_intelligence import enrich_with_price_intelligence


# ---------------- SETTINGS ----------------

STRONG_DIP_PERCENT = getattr(
    config,
    "STRONG_DIP_PERCENT",
    -30.0,
)

STRONG_PUMP_PERCENT = getattr(
    config,
    "STRONG_PUMP_PERCENT",
    30.0,
)

CHEAP_MARKET_MAX_PRICE = getattr(
    config,
    "CHEAP_MARKET_MAX_PRICE",
    0.01,  # до 1¢
)

CHEAP_MARKET_MIN_MOVE = getattr(
    config,
    "CHEAP_MARKET_MIN_MOVE",
    0.002,  # минимум 0.2¢
)

NORMAL_MARKET_MIN_MOVE = getattr(
    config,
    "NORMAL_MARKET_MIN_MOVE",
    0.02,  # минимум 2¢
)

VALUE_MAX_PRICE = getattr(
    config,
    "VALUE_MAX_PRICE",
    0.03,
)

VALUE_MIN_LIQUIDITY = getattr(
    config,
    "VALUE_MIN_LIQUIDITY",
    500_000,
)

VALUE_MIN_SCORE = getattr(
    config,
    "VALUE_MIN_SCORE",
    80,
)

ALERT_COOLDOWN_HOURS = getattr(
    config,
    "ALERT_COOLDOWN_HOURS",
    24,
)

AUTO_VALUE_ALERTS = getattr(
    config,
    "AUTO_VALUE_ALERTS",
    False,
)


# ---------------- HELPERS ----------------

def absolute_move(
    current_price: float,
    old_price: Optional[float],
) -> float:
    if old_price is None:
        return 0.0

    return abs(current_price - old_price)


def required_absolute_move(
    current_price: float,
    old_price: Optional[float],
) -> float:
    """
    Для дешёвых рынков используем меньший абсолютный порог.

    Рынок считается дешёвым, если текущая или предыдущая
    цена не превышает 1¢.
    """
    reference_price = max(
        current_price,
        old_price or 0.0,
    )

    if reference_price <= CHEAP_MARKET_MAX_PRICE:
        return CHEAP_MARKET_MIN_MOVE

    return NORMAL_MARKET_MIN_MOVE


def get_timeframes(
    signal: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "name": "5 минут",
            "code": "5M",
            "change": signal.get("change_5m"),
            "old_price": signal.get("price_5m"),
            "volume_change_percent": signal.get("volume_change_5m"),
            "liquidity_change_percent": signal.get("liquidity_change_5m"),
        },
        {
            "name": "15 минут",
            "code": "15M",
            "change": signal.get("change_15m"),
            "old_price": signal.get("price_15m"),
            "volume_change_percent": signal.get("volume_change_15m"),
            "liquidity_change_percent": signal.get("liquidity_change_15m"),
        },
        {
            "name": "1 час",
            "code": "1H",
            "change": signal.get("change_1h"),
            "old_price": signal.get("price_1h"),
            "volume_change_percent": signal.get("volume_change_1h"),
            "liquidity_change_percent": signal.get("liquidity_change_1h"),
        },
        {
            "name": "24 часа",
            "code": "24H",
            "change": signal.get("change_24h"),
            "old_price": signal.get("price_24h"),
            "volume_change_percent": signal.get("volume_change_24h"),
            "liquidity_change_percent": signal.get("liquidity_change_24h"),
        },
    ]


def find_strongest_drop(
    signal: dict[str, Any],
) -> Optional[dict[str, Any]]:
    valid = [
        item
        for item in get_timeframes(signal)
        if item["change"] is not None
        and item["old_price"] is not None
    ]

    if not valid:
        return None

    return min(
        valid,
        key=lambda item: item["change"],
    )


def find_strongest_pump(
    signal: dict[str, Any],
) -> Optional[dict[str, Any]]:
    valid = [
        item
        for item in get_timeframes(signal)
        if item["change"] is not None
        and item["old_price"] is not None
    ]

    if not valid:
        return None

    return max(
        valid,
        key=lambda item: item["change"],
    )


# ---------------- ALERT DETECTION ----------------

def detect_strong_dip(
    signal: dict[str, Any],
) -> Optional[dict[str, Any]]:
    timeframe = find_strongest_drop(signal)

    if timeframe is None:
        return None

    change = float(timeframe["change"])
    old_price = float(timeframe["old_price"])
    current_price = float(signal["price"])

    # Например, при пороге -30:
    # -50 проходит, а -20 не проходит.
    if change > STRONG_DIP_PERCENT:
        return None

    move = absolute_move(
        current_price,
        old_price,
    )

    required_move = required_absolute_move(
        current_price,
        old_price,
    )

    if move < required_move:
        return None

    return {
        "alert_type": (
            f"STRONG_DIP_{timeframe['code']}"
        ),
        "alert_label": "🔴 STRONG DIP",
        "timeframe": timeframe["name"],
        "change_percent": change,
        "volume_change_percent": timeframe.get("volume_change_percent"),
        "liquidity_change_percent": timeframe.get("liquidity_change_percent"),
        "old_price": old_price,
        "current_price": current_price,
        "absolute_move": move,
        "required_move": required_move,
    }


def detect_strong_pump(
    signal: dict[str, Any],
) -> Optional[dict[str, Any]]:
    timeframe = find_strongest_pump(signal)

    if timeframe is None:
        return None

    change = float(timeframe["change"])
    old_price = float(timeframe["old_price"])
    current_price = float(signal["price"])

    if change < STRONG_PUMP_PERCENT:
        return None

    move = absolute_move(
        current_price,
        old_price,
    )

    required_move = required_absolute_move(
        current_price,
        old_price,
    )

    if move < required_move:
        return None

    return {
        "alert_type": (
            f"STRONG_PUMP_{timeframe['code']}"
        ),
        "alert_label": "🚀 STRONG PUMP",
        "timeframe": timeframe["name"],
        "change_percent": change,
        "volume_change_percent": timeframe.get("volume_change_percent"),
        "liquidity_change_percent": timeframe.get("liquidity_change_percent"),
        "old_price": old_price,
        "current_price": current_price,
        "absolute_move": move,
        "required_move": required_move,
    }


def detect_value(
    signal: dict[str, Any],
) -> Optional[dict[str, Any]]:
    price = float(signal["price"])
    liquidity = float(signal["liquidity"])
    score = int(signal["score"])

    if price > VALUE_MAX_PRICE:
        return None

    if liquidity < VALUE_MIN_LIQUIDITY:
        return None

    if score < VALUE_MIN_SCORE:
        return None

    return {
        "alert_type": "VALUE",
        "alert_label": "💎 VALUE OPPORTUNITY",
        "timeframe": None,
        "change_percent": None,
        "old_price": signal.get("previous_price"),
        "current_price": price,
        "absolute_move": 0.0,
        "required_move": 0.0,
    }


def detect_alerts(
    signal: dict[str, Any],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []

    dip = detect_strong_dip(signal)

    if dip is not None:
        alerts.append(dip)

    pump = detect_strong_pump(signal)

    if pump is not None:
        alerts.append(pump)

    if AUTO_VALUE_ALERTS:
        value = detect_value(signal)

        if value is not None:
            alerts.append(value)

    return alerts


# ---------------- DEDUPLICATION ----------------

def get_min_alert_liquidity(signal: dict[str, Any]) -> float:
    category = str(signal.get("category") or "").upper()

    if "POLITICS" in category:
        return float(config.MIN_ALERT_LIQUIDITY_POLITICS)

    if "CRYPTO" in category:
        return float(config.MIN_ALERT_LIQUIDITY_CRYPTO)

    if "SPORTS" in category:
        return float(config.MIN_ALERT_LIQUIDITY_SPORTS)

    if (
        "ENTERTAINMENT" in category
        or "CULTURE" in category
        or "CELEBRITY" in category
    ):
        return float(config.MIN_ALERT_LIQUIDITY_ENTERTAINMENT)

    return float(config.MIN_ALERT_LIQUIDITY_DEFAULT)

def check_signals(
    signals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []

    for signal in signals:
        liquidity = float(signal.get("liquidity") or 0)
        min_liquidity = get_min_alert_liquidity(signal)

        if liquidity < min_liquidity:
            continue

        market_id = str(signal.get("id") or "")
        if not market_id:
            continue

        for alert_data in detect_alerts(signal):
            alert_type = str(alert_data["alert_type"])

            prepared_alert = enrich_signal(
                {
                    **signal,
                    **alert_data,
                }
            )
            prepared_alert.update(analyze_similarity(prepared_alert))
            prepared_alert.update(calibrate_signal(prepared_alert))
            prepared_alert = enrich_with_price_intelligence(prepared_alert)
            prepared_alert = enrich_with_confidence(prepared_alert)
            candidates.append(prepared_alert)

    # Markets belonging to one Polymarket event are treated as one family.
    # Only the strongest representative is stored and sent, so mutually
    # exclusive outcomes do not distort AI Memory or flood subscribers.
    grouped_candidates = select_group_representatives(candidates)
    new_alerts: list[dict[str, Any]] = []
    group_cooldown_hours = float(
        getattr(config, "MARKET_GROUP_COOLDOWN_HOURS", 24)
    )

    for prepared_alert in grouped_candidates:
        market_id = str(prepared_alert.get("id") or "")
        alert_type = str(prepared_alert.get("alert_type") or "")
        group_key = get_market_group_key(prepared_alert)

        cooldown = check_smart_cooldown(prepared_alert)
        if cooldown.blocked:
            continue

        save_alert(market_id, alert_type)
        save_group_alert(group_key, "ANY_ALERT", market_id)
        register_smart_cooldown(prepared_alert)

        try:
            prepared_alert["ai_signal_id"] = record_alert(prepared_alert)
        except Exception:
            # AI Data Layer не должен останавливать рабочие алерты.
            prepared_alert["ai_signal_id"] = None

        new_alerts.append(prepared_alert)

        # Quality Live v2 is shadow-only: record the decision but never change
        # the real alert stream. Recording starts only after this deployment.
        try:
            record_shadow_decision(prepared_alert)
        except Exception:
            pass

    # Quality Engine v3 — LIVE high-precision gate.
    # It runs AFTER record_alert(), therefore AI Memory keeps both accepted and
    # rejected candidates. This changes only Telegram delivery, not learning.
    if bool(getattr(config, "QUALITY_ENGINE_V3_MODE", True)):
        quality_alerts: list[dict[str, Any]] = []
        for item in new_alerts:
            passed, reason, confirmations = evaluate_quality_v3(item)
            item["quality_v3_confirmations"] = confirmations
            if not passed:
                item["quality_live_block_reason"] = reason
                record_quality_decision(item, False, reason, engine_version="v3")
                continue
            item["quality_live_passed"] = True
            item["quality_live_version"] = "v3"
            record_quality_decision(item, True, engine_version="v3")
            quality_alerts.append(item)
        new_alerts = quality_alerts

    priority = {
        "💎 VALUE OPPORTUNITY": 3,
        "🔴 STRONG DIP": 2,
        "🚀 STRONG PUMP": 1,
    }

    new_alerts.sort(
        key=lambda item: (
            -priority.get(item["alert_label"], 0),
            -abs(item.get("change_percent") or 0),
            -item["score"],
        )
    )

    return new_alerts

# ---------------- TELEGRAM FORMAT ----------------

def format_alert(
    alert: dict[str, Any],
) -> str:
    return format_calibrated_alert(alert, opportunity=False)


# ---------------- MANUAL TEST ----------------

if __name__ == "__main__":
    from scanner import scan

    markets = scan()
    alerts = check_signals(markets)

    print(
        "\nНовых важных алертов: "
        f"{len(alerts)}\n"
    )

    for item in alerts:
        print("=" * 70)
        print(format_alert(item))
        print()
