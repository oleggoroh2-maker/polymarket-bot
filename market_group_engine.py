from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable

import config


MARKET_GROUPING_ENABLED = bool(
    getattr(config, "MARKET_GROUPING_ENABLED", True)
)
MARKET_GROUP_MAX_ALERTS_PER_SCAN = int(
    getattr(config, "MARKET_GROUP_MAX_ALERTS_PER_SCAN", 1)
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize(value: Any) -> str:
    text = _text(value).lower()
    text = re.sub(r"[^a-z0-9а-яё]+", "-", text, flags=re.IGNORECASE)
    return text.strip("-")


def get_market_group_key(market: dict[str, Any]) -> str:
    """Return a stable event-level key for mutually related markets.

    The Gamma events endpoint already supplies event_slug/event metadata. We
    prefer those values and fall back to the market id so unrelated markets
    are never accidentally merged.
    """
    for key in ("event_slug", "eventSlug"):
        value = _normalize(market.get(key))
        if value:
            return f"event:{value}"

    event = market.get("event")
    if isinstance(event, dict):
        for key in ("slug", "id", "title"):
            value = _normalize(event.get(key))
            if value:
                return f"event:{value}"

    for key in ("event_id", "eventId", "event_title"):
        value = _normalize(market.get(key))
        if value:
            return f"event:{value}"

    market_id = _normalize(market.get("id"))
    return f"market:{market_id}" if market_id else "market:unknown"


def get_market_group_title(market: dict[str, Any]) -> str:
    event_title = _text(market.get("event_title"))
    if event_title:
        return event_title

    event = market.get("event")
    if isinstance(event, dict):
        title = _text(event.get("title"))
        if title:
            return title

    return _text(market.get("title"))


def _candidate_rank(candidate: dict[str, Any]) -> tuple[float, ...]:
    """Rank one representative inside an event group.

    Calibration and similarity are used only as quality signals. The engine
    does not alter their values; it merely chooses the strongest candidate
    from a family of related outcomes.
    """
    return (
        float(candidate.get("calibration_score") or 0.0),
        float(candidate.get("ai_quality") or 0.0),
        float(candidate.get("score") or 0.0),
        float(candidate.get("similarity_strong_rate") or 0.0),
        abs(float(candidate.get("change_percent") or 0.0)),
        float(candidate.get("liquidity") or 0.0),
    )


def select_group_representatives(
    candidates: Iterable[dict[str, Any]],
    *,
    max_per_group: int | None = None,
) -> list[dict[str, Any]]:
    """Keep the best candidate(s) from each Polymarket event group."""
    items = list(candidates)
    if not MARKET_GROUPING_ENABLED:
        return items

    limit = (
        MARKET_GROUP_MAX_ALERTS_PER_SCAN
        if max_per_group is None
        else max(1, int(max_per_group))
    )

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        group_key = get_market_group_key(item)
        item["market_group_key"] = group_key
        item["market_group_title"] = get_market_group_title(item)
        grouped[group_key].append(item)

    selected: list[dict[str, Any]] = []
    for group_items in grouped.values():
        group_items.sort(key=_candidate_rank, reverse=True)
        group_size = len(group_items)
        for item in group_items[:limit]:
            item["market_group_size"] = group_size
            item["market_group_suppressed"] = max(0, group_size - limit)
            selected.append(item)

    return selected
