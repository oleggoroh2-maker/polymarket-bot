from typing import Any, Optional

import config

from database import (
    alert_on_cooldown,
    save_alert,
)


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

MIN_ABSOLUTE_MOVE = getattr(
    config,
    "MIN_ABSOLUTE_MOVE",
    0.02,  # 2 цента
)

VALUE_MAX_PRICE = getattr(
    config,
    "VALUE_MAX_PRICE",
    0.03,  # 3 цента
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


def get_timeframes(
    signal: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "name": "5 минут",
            "code": "5M",
            "change": signal.get("change_5m"),
            "old_price": signal.get("price_5m"),
        },
        {
            "name": "15 минут",
            "code": "15M",
            "change": signal.get("change_15m"),
            "old_price": signal.get("price_15m"),
        },
        {
            "name": "1 час",
            "code": "1H",
            "change": signal.get("change_1h"),
            "old_price": signal.get("price_1h"),
        },
        {
            "name": "24 часа",
            "code": "24H",
            "change": signal.get("change_24h"),
            "old_price": signal.get("price_24h"),
        },
    ]


def find_strongest_drop(
    signal: dict[str, Any],
) -> Optional[dict[str, Any]]:
    valid = [
        item
        for item in get_timeframes(signal)
        if item["change"] is not None
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
    old_price = timeframe["old_price"]
    current_price = float(signal["price"])

    move = absolute_move(
        current_price,
        old_price,
    )

    if change > STRONG_DIP_PERCENT:
        return None

    if move < MIN_ABSOLUTE_MOVE:
        return None

    return {
        "alert_type": (
            f"STRONG_DIP_{timeframe['code']}"
        ),
        "alert_label": "🔴 STRONG DIP",
        "timeframe": timeframe["name"],
        "change_percent": change,
        "old_price": old_price,
        "current_price": current_price,
        "absolute_move": move,
    }


def detect_strong_pump(
    signal: dict[str, Any],
) -> Optional[dict[str, Any]]:
    timeframe = find_strongest_pump(signal)

    if timeframe is None:
        return None

    change = float(timeframe["change"])
    old_price = timeframe["old_price"]
    current_price = float(signal["price"])

    move = absolute_move(
        current_price,
        old_price,
    )

    if change < STRONG_PUMP_PERCENT:
        return None

    if move < MIN_ABSOLUTE_MOVE:
        return None

    return {
        "alert_type": (
            f"STRONG_PUMP_{timeframe['code']}"
        ),
        "alert_label": "🚀 STRONG PUMP",
        "timeframe": timeframe["name"],
        "change_percent": change,
        "old_price": old_price,
        "current_price": current_price,
        "absolute_move": move,
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
        "old_price": signal.get(
            "previous_price"
        ),
        "current_price": price,
        "absolute_move": 0.0,
    }


def detect_alerts(
    signal: dict[str, Any],
) -> list[dict[str, Any]]:
    alerts = []

    dip = detect_strong_dip(signal)

    if dip is not None:
        alerts.append(dip)

    pump = detect_strong_pump(signal)

    if pump is not None:
        alerts.append(pump)

    value = detect_value(signal)

    if AUTO_VALUE_ALERTS:
        value = detect_value(signal)

        if value is not None:
            alerts.append(value)

    return alerts


# ---------------- DEDUPLICATION ----------------

def check_signals(
    signals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    new_alerts = []

    for signal in signals:
        market_id = str(signal["id"])

        detected = detect_alerts(signal)

        for alert_data in detected:
            alert_type = alert_data["alert_type"]

            if alert_on_cooldown(
                market_id,
                alert_type,
                ALERT_COOLDOWN_HOURS,
            ):
                continue

            save_alert(
                market_id,
                alert_type,
            )

            prepared_alert = {
                **signal,
                **alert_data,
            }

            new_alerts.append(
                prepared_alert
            )

    priority = {
        "💎 VALUE OPPORTUNITY": 3,
        "🔴 STRONG DIP": 2,
        "🚀 STRONG PUMP": 1,
    }

    new_alerts.sort(
        key=lambda item: (
            -priority.get(
                item["alert_label"],
                0,
            ),
            -abs(
                item.get(
                    "change_percent"
                ) or 0
            ),
            -item["score"],
        )
    )

    return new_alerts


# ---------------- TELEGRAM FORMAT ----------------

def format_alert(
    alert: dict[str, Any],
) -> str:
    lines = [
        alert["alert_label"],
        "",
        f"📊 {alert['title']}",
        "",
    ]

    old_price = alert.get("old_price")
    current_price = alert["current_price"]

    if old_price is not None:
        lines.append(
            "💰 Цена: "
            f"{old_price * 100:.2f}¢ → "
            f"{current_price * 100:.2f}¢"
        )
    else:
        lines.append(
            "💰 Цена: "
            f"{current_price * 100:.2f}¢"
        )

    change = alert.get("change_percent")

    if change is not None:
        lines.append(
            f"📈 Изменение: {change:+.1f}%"
        )

    timeframe = alert.get("timeframe")

    if timeframe:
        lines.append(
            f"⏱ Период: {timeframe}"
        )

    lines.extend(
        [
            f"💧 Ликвидность: "
            f"${alert['liquidity']:,.0f}",
            f"⭐ Score: {alert['score']}/100",
            f"🏷 {alert['category']}",
            f"⏳ {alert['days_left']} дней",
        ]
    )

    url = alert.get("url")

    if url:
        lines.extend(
            [
                "",
                f"🌐 {url}",
            ]
        )

    return "\n".join(lines)


if __name__ == "__main__":
    from scanner import scan

    markets = scan()
    alerts = check_signals(markets)

    print(
        f"\nНовых важных алертов: "
        f"{len(alerts)}\n"
    )

    for item in alerts:
        print("=" * 70)
        print(format_alert(item))
        print()