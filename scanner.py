import logging
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from database import (
    cleanup_alerts,
    cleanup_prices,
    get_latest_price,
    get_price_before,
    init_db,
    save_price,
)

# ---------------- SETTINGS ----------------

API_URL = "https://gamma-api.polymarket.com/markets"
API_LIMIT = 1000
REQUEST_TIMEOUT = 30

MIN_LIQUIDITY = 10
MIN_DAYS_LEFT = 30

PRICE_HISTORY_DAYS = 30
ALERT_HISTORY_DAYS = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ---------------- API ----------------

def get_markets() -> list[dict[str, Any]]:
    response = requests.get(
        API_URL,
        params={
            "closed": "false",
            "limit": API_LIMIT,
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, list):
        raise ValueError(
            "Polymarket API вернул данные в неожиданном формате."
        )

    return data


# ---------------- HELPERS ----------------

def parse_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_end_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def calculate_change(
    current_price: float,
    old_price: Optional[float],
) -> Optional[float]:
    if old_price is None or old_price <= 0:
        return None

    return (
        (current_price - old_price)
        / old_price
    ) * 100


def calculate_absolute_change(
    current_price: float,
    old_price: Optional[float],
) -> Optional[float]:
    if old_price is None:
        return None

    return current_price - old_price


def detect_category(title: str) -> str:
    text = title.lower()

    if any(word in text for word in (
        "bitcoin",
        "btc",
        "ethereum",
        "eth",
        "solana",
        "xrp",
        "dogecoin",
        "crypto",
    )):
        return "₿ CRYPTO"

    if any(word in text for word in (
        "ai",
        "openai",
        "anthropic",
        "chatgpt",
        "nvidia",
        "tesla",
        "spacex",
    )):
        return "🤖 AI/TECH"

    if any(word in text for word in (
        "etf",
        "fund",
        "blackrock",
        "fidelity",
    )):
        return "📈 ETF"

    if any(word in text for word in (
        "election",
        "president",
        "presidential",
        "democratic",
        "republican",
        "senate",
        "governor",
        "congress",
        "trump",
    )):
        return "🏛 POLITICS"

    if any(word in text for word in (
        "nba",
        "nfl",
        "nhl",
        "mlb",
        "football",
        "soccer",
        "championship",
        "world cup",
    )):
        return "⚽ SPORTS"

    return "📦 OTHER"


BAD_MARKET_WORDS = (
    "prison",
    "sentenced",
    "jail",
    "criminal",
    "convicted",
    "harvey weinstein",
)


def is_bad_market(title: str) -> bool:
    text = title.lower()

    return any(
        word in text
        for word in BAD_MARKET_WORDS
    )


def detect_momentum(
    change_5m: Optional[float],
    change_15m: Optional[float],
    change_1h: Optional[float],
    change_24h: Optional[float],
) -> str:
    changes = [
        value
        for value in (
            change_5m,
            change_15m,
            change_1h,
            change_24h,
        )
        if value is not None
    ]

    if not changes:
        return "🆕 NEW"

    strongest_drop = min(changes)
    strongest_growth = max(changes)

    if strongest_drop <= -50:
        return "🔥 STRONG DIP"

    if strongest_drop <= -30:
        return "📉 DIP"

    if strongest_growth >= 50:
        return "🚀 STRONG PUMP"

    if strongest_growth >= 30:
        return "📈 GROWTH"

    return "⚪ FLAT"


def calculate_score(
    price: float,
    liquidity: float,
    days_left: int,
    change_5m: Optional[float],
    change_15m: Optional[float],
    change_1h: Optional[float],
    change_24h: Optional[float],
) -> int:
    score = 0

    # Цена
    if price <= 0.01:
        score += 35
    elif price <= 0.03:
        score += 28
    elif price <= 0.05:
        score += 20
    elif price <= 0.10:
        score += 12
    else:
        score += 5

    # Ликвидность
    if liquidity >= 1_000_000:
        score += 25
    elif liquidity >= 500_000:
        score += 20
    elif liquidity >= 100_000:
        score += 15
    elif liquidity >= 10_000:
        score += 10
    elif liquidity >= 1_000:
        score += 5

    # Срок рынка
    if days_left >= 730:
        score += 15
    elif days_left >= 365:
        score += 12
    elif days_left >= 180:
        score += 8
    elif days_left >= 90:
        score += 5

    # Momentum
    changes = [
        value
        for value in (
            change_5m,
            change_15m,
            change_1h,
            change_24h,
        )
        if value is not None
    ]

    if changes:
        strongest_move = max(
            changes,
            key=abs,
        )

        if abs(strongest_move) >= 50:
            score += 25
        elif abs(strongest_move) >= 30:
            score += 18
        elif abs(strongest_move) >= 20:
            score += 10

    return min(score, 100)


def get_primary_change(
    change_5m: Optional[float],
    change_15m: Optional[float],
    change_1h: Optional[float],
    change_24h: Optional[float],
) -> float:
    for value in (
        change_5m,
        change_15m,
        change_1h,
        change_24h,
    ):
        if value is not None:
            return value

    return 0.0


def build_market_url(market: dict[str, Any]) -> Optional[str]:
    slug = market.get("slug")

    if not slug:
        return None

    return f"https://polymarket.com/event/{slug}"


# ---------------- SCANNER ----------------

def scan() -> list[dict[str, Any]]:
    init_db()
    cleanup_prices(PRICE_HISTORY_DAYS)
    cleanup_alerts(ALERT_HISTORY_DAYS)

    markets = get_markets()
    results: list[dict[str, Any]] = []

    for market in markets:
        try:
            market_id = str(
                market.get("id")
                or market.get("slug")
                or ""
            ).strip()

            if not market_id:
                continue

            title = str(
                market.get("question")
                or ""
            ).strip()

            if not title:
                continue

            if is_bad_market(title):
                continue

            price = parse_float(
                market.get("lastTradePrice")
            )

            if price <= 0 or price >= 1:
                continue

            liquidity = parse_float(
                market.get("liquidityNum")
            )

            if liquidity < MIN_LIQUIDITY:
                continue

            end_date = parse_end_date(
                market.get("endDate")
            )

            if end_date is None:
                continue

            days_left = (
                end_date
                - datetime.now(timezone.utc)
            ).days

            if days_left < MIN_DAYS_LEFT:
                continue

            previous_price = get_latest_price(
                market_id
            )

            price_5m = get_price_before(
                market_id,
                5,
            )

            price_15m = get_price_before(
                market_id,
                15,
            )

            price_1h = get_price_before(
                market_id,
                60,
            )

            price_24h = get_price_before(
                market_id,
                1440,
            )

            change_5m = calculate_change(
                price,
                price_5m,
            )

            change_15m = calculate_change(
                price,
                price_15m,
            )

            change_1h = calculate_change(
                price,
                price_1h,
            )

            change_24h = calculate_change(
                price,
                price_24h,
            )

            momentum = detect_momentum(
                change_5m,
                change_15m,
                change_1h,
                change_24h,
            )

            score = calculate_score(
                price,
                liquidity,
                days_left,
                change_5m,
                change_15m,
                change_1h,
                change_24h,
            )

            primary_change = get_primary_change(
                change_5m,
                change_15m,
                change_1h,
                change_24h,
            )

            results.append(
                {
                    "id": market_id,
                    "slug": market.get("slug"),
                    "title": title,
                    "price": price,
                    "previous_price": previous_price,
                    "price_5m": price_5m,
                    "price_15m": price_15m,
                    "price_1h": price_1h,
                    "price_24h": price_24h,
                    "change": primary_change,
                    "change_5m": change_5m,
                    "change_15m": change_15m,
                    "change_1h": change_1h,
                    "change_24h": change_24h,
                    "absolute_change": (
                        calculate_absolute_change(
                            price,
                            previous_price,
                        )
                    ),
                    "liquidity": liquidity,
                    "days_left": days_left,
                    "category": detect_category(
                        title
                    ),
                    "momentum": momentum,
                    "score": score,
                    "url": build_market_url(
                        market
                    ),
                }
            )

            # Сохраняем текущую цену после чтения истории
            save_price(
                market_id,
                price,
            )

        except Exception:
            logger.exception(
                "Ошибка обработки рынка: %s",
                market.get("question"),
            )

    results.sort(
        key=lambda item: (
            -item["score"],
            -abs(item["change"]),
            -item["liquidity"],
        )
    )

    return results


# ---------------- CONSOLE ----------------

def format_change(
    value: Optional[float],
) -> str:
    if value is None:
        return "нет истории"

    return f"{value:+.1f}%"


def print_signals(
    signals: list[dict[str, Any]],
    limit: int = 30,
) -> None:
    print("\n🚀 POLYMARKET MOMENTUM SCANNER\n")
    print(f"Найдено рынков: {len(signals)}\n")

    for signal in signals[:limit]:
        print("=" * 70)
        print(
            f"{signal['momentum']} | "
            f"SCORE {signal['score']}/100"
        )
        print(f"📊 {signal['title']}")
        print(f"🏷 {signal['category']}")
        print(
            f"💰 Цена: "
            f"{signal['price'] * 100:.2f}¢"
        )
        print(
            f"💧 Ликвидность: "
            f"${signal['liquidity']:,.0f}"
        )
        print(
            f"⏳ До окончания: "
            f"{signal['days_left']} дней"
        )
        print(
            f"5м: {format_change(signal['change_5m'])}"
        )
        print(
            f"15м: {format_change(signal['change_15m'])}"
        )
        print(
            f"1ч: {format_change(signal['change_1h'])}"
        )
        print(
            f"24ч: {format_change(signal['change_24h'])}"
        )

        if signal["url"]:
            print(f"🌐 {signal['url']}")

        print()


if __name__ == "__main__":
    try:
        scanned_signals = scan()
        print_signals(scanned_signals)
    except requests.RequestException as error:
        logger.error(
            "Ошибка подключения к Polymarket API: %s",
            error,
        )
    except Exception:
        logger.exception(
            "Критическая ошибка сканера."
        )