"""Multi-tier Telegram routing.

TRADE preserves the existing strict Quality v3 + EV/Risk gate.
WATCH and MARKET_MOVE are informational only: they increase observability and
future statistics without weakening Trade Intelligence decisions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any

import config

_lock = Lock()
_day = ""
_counts = {"WATCH": 0, "MARKET_MOVE": 0}


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def classify_alert_route(alert: dict[str, Any]) -> str | None:
    """Return TRADE, WATCH, MARKET_MOVE or None. Does not consume quota."""
    quality_ok = bool(alert.get("quality_live_passed"))
    ev_ok = bool(alert.get("ev_risk_passed"))
    if quality_ok and ev_ok:
        return "TRADE"

    final = _num(alert.get("final_signal_score"))
    confidence = _num(alert.get("signal_confidence"))
    ai_quality = _num(alert.get("ai_quality"))
    risk = _num(alert.get("risk_score"), 100.0)
    ev = _num(alert.get("ev_estimate_percent"), -100.0)
    change = abs(_num(alert.get("change_percent")))
    volume = abs(_num(alert.get("volume_change_percent")))
    similarity = _num(alert.get("similarity_average"))

    # Strong but rejected candidates: useful to see, but explicitly INFO.
    watch = (
        final >= float(getattr(config, "ALERT_ROUTING_WATCH_FINAL_MIN", 72.0))
        and risk <= float(getattr(config, "ALERT_ROUTING_WATCH_RISK_MAX", 75.0))
        and (confidence >= 48.0 or ai_quality >= 48.0 or similarity >= 65.0)
        and ev >= -12.0
    )
    if watch:
        return "WATCH"

    # Exceptional tape activity that is worth observing even when AI dislikes
    # the entry. This is not a trade recommendation.
    move = (
        change >= float(getattr(config, "ALERT_ROUTING_MOVE_CHANGE_MIN", 45.0))
        or (
            volume >= float(getattr(config, "ALERT_ROUTING_MOVE_VOLUME_MIN", 1000.0))
            and change >= 20.0
        )
    )
    if move and final >= 55.0:
        return "MARKET_MOVE"
    return None


def claim_info_slot(route: str) -> bool:
    """Process-local daily flood guard for INFO tiers. TRADE is unlimited."""
    global _day, _counts
    if route == "TRADE":
        return True
    if route not in _counts:
        return False
    today = datetime.now(timezone.utc).date().isoformat()
    with _lock:
        if _day != today:
            _day = today
            _counts = {"WATCH": 0, "MARKET_MOVE": 0}
        limit = (
            int(getattr(config, "ALERT_ROUTING_WATCH_DAILY_MAX", 12))
            if route == "WATCH"
            else int(getattr(config, "ALERT_ROUTING_MOVE_DAILY_MAX", 4))
        )
        if _counts[route] >= limit:
            return False
        _counts[route] += 1
        return True


def route_label(route: str) -> str:
    return {
        "TRADE": "🔥 TRADE",
        "WATCH": "🟡 WATCH · INFO",
        "MARKET_MOVE": "👀 MARKET MOVE · INFO",
    }.get(route, route)
