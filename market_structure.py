"""Fetch real Polymarket CLOB order-book metrics for alert messages.

The module is deliberately best-effort: API failures never stop scanning or
Telegram delivery. Missing values are simply omitted by the formatter.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

import requests


CLOB_BOOKS_URL = "https://clob.polymarket.com/books"
REQUEST_TIMEOUT = 12
logger = logging.getLogger(__name__)


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _levels(book: dict[str, Any], side: str) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    raw_levels = book.get(side)
    if not isinstance(raw_levels, list):
        return result

    for item in raw_levels:
        if not isinstance(item, dict):
            continue
        price = _num(item.get("price"))
        size = _num(item.get("size"))
        if price is None or size is None or price < 0 or size < 0:
            continue
        result.append((price, size))
    return result


def _book_metrics(book: dict[str, Any]) -> dict[str, Optional[float]]:
    bids = _levels(book, "bids")
    asks = _levels(book, "asks")

    best_bid = max((price for price, _ in bids), default=None)
    best_ask = min((price for price, _ in asks), default=None)
    spread = (
        best_ask - best_bid
        if best_bid is not None and best_ask is not None
        else None
    )

    bid_depth = sum(price * size for price, size in bids)
    ask_depth = sum(price * size for price, size in asks)
    total_depth = bid_depth + ask_depth
    bid_balance = (
        bid_depth / total_depth * 100.0
        if total_depth > 0
        else None
    )
    largest_order = max(
        [price * size for price, size in bids + asks],
        default=None,
    )

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "bid_depth": bid_depth if bids else None,
        "ask_depth": ask_depth if asks else None,
        "bid_balance": bid_balance,
        "largest_order": largest_order,
    }


def _outcome_mapping(alert: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes = [str(item) for item in _list(alert.get("outcomes"))]
    prices = _list(alert.get("outcome_prices"))
    token_ids = [str(item) for item in _list(alert.get("clob_token_ids"))]

    count = max(len(outcomes), len(prices), len(token_ids))
    rows: list[dict[str, Any]] = []
    for index in range(count):
        rows.append(
            {
                "name": outcomes[index] if index < len(outcomes) else str(index + 1),
                "price": _num(prices[index]) if index < len(prices) else None,
                "token_id": token_ids[index] if index < len(token_ids) else None,
            }
        )
    return rows


def enrich_market_structure(alert: dict[str, Any]) -> dict[str, Any]:
    """Return alert with factual outcome prices and order-book statistics."""
    enriched = dict(alert)
    rows = _outcome_mapping(alert)

    for row in rows:
        name = str(row["name"]).strip().lower()
        if name == "yes":
            enriched["yes_price"] = row["price"]
            enriched["yes_token_id"] = row["token_id"]
        elif name == "no":
            enriched["no_price"] = row["price"]
            enriched["no_token_id"] = row["token_id"]

    token_rows = [row for row in rows if row.get("token_id")]
    if not token_rows or not bool(alert.get("enable_order_book", True)):
        return enriched

    try:
        response = requests.post(
            CLOB_BOOKS_URL,
            json=[{"token_id": row["token_id"]} for row in token_rows],
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            return enriched
    except Exception as error:
        logger.info("CLOB structure unavailable for %s: %s", alert.get("id"), error)
        return enriched

    for row, book in zip(token_rows, payload):
        if not isinstance(book, dict):
            continue
        metrics = _book_metrics(book)
        name = str(row["name"]).strip().lower()
        prefix = "yes" if name == "yes" else "no" if name == "no" else None
        if prefix is None:
            continue
        for key, value in metrics.items():
            enriched[f"{prefix}_{key}"] = value

    # Main tradable-side metrics shown without a prefix use YES when available.
    prefix = "yes" if enriched.get("yes_token_id") else "no"
    for key in (
        "best_bid",
        "best_ask",
        "spread",
        "bid_depth",
        "ask_depth",
        "bid_balance",
        "largest_order",
    ):
        value = enriched.get(f"{prefix}_{key}")
        if value is not None:
            enriched[key] = value

    return enriched
