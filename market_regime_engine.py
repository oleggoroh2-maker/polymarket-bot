"""Market Regime Engine v1 (shadow).

Classifies each delivered alert without changing delivery decisions.
The classifier intentionally uses only information already present in the alert,
so it remains deterministic and auditable.
"""
from __future__ import annotations
from typing import Any

REGIMES = ("NORMAL", "MOMENTUM", "EVENT_SHOCK", "CHAOS_MANIPULATION")

def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None

def classify_market_regime(alert: dict[str, Any]) -> dict[str, Any]:
    move = abs(_num(alert.get("change_percent")) or 0.0)
    vol = abs(_num(alert.get("volume_change_percent")) or 0.0)
    liq = _num(alert.get("liquidity")) or 0.0
    liq_delta = _num(alert.get("liquidity_change_percent")) or 0.0
    spread = _num(alert.get("spread"))
    price = _num(alert.get("current_price", alert.get("price"))) or 0.0
    bid_depth = _num(alert.get("bid_depth")) or 0.0
    ask_depth = _num(alert.get("ask_depth")) or 0.0
    largest = _num(alert.get("largest_order")) or 0.0

    spread_pct = (spread / price * 100.0) if spread is not None and price > 0 else 0.0
    depth = bid_depth + ask_depth
    concentration = (largest / depth * 100.0) if depth > 0 else 0.0

    chaos = 0
    shock = 0
    momentum = 0
    reasons: list[str] = []

    if spread_pct >= 8: chaos += 3; reasons.append(f"wide spread {spread_pct:.1f}%")
    elif spread_pct >= 4: chaos += 1
    if concentration >= 55: chaos += 2; reasons.append(f"order concentration {concentration:.0f}%")
    if move >= 45 and vol < 30: chaos += 2; reasons.append("large move without volume confirmation")
    if liq and liq < 10_000 and move >= 25: chaos += 2; reasons.append("thin liquidity + large move")

    if move >= 35: shock += 2
    if move >= 60: shock += 2
    if vol >= 150: shock += 2
    if abs(liq_delta) >= 40: shock += 1
    if shock >= 4: reasons.append("abrupt price/flow shock")

    if move >= 15: momentum += 1
    if vol >= 80: momentum += 2
    elif vol >= 30: momentum += 1
    if liq_delta >= 10: momentum += 1
    if spread_pct and spread_pct <= 3: momentum += 1
    if momentum >= 3: reasons.append("price move confirmed by flow")

    if chaos >= 4:
        regime, score = "CHAOS_MANIPULATION", min(100, 50 + chaos * 8)
    elif shock >= 4:
        regime, score = "EVENT_SHOCK", min(100, 50 + shock * 8)
    elif momentum >= 3:
        regime, score = "MOMENTUM", min(100, 50 + momentum * 8)
    else:
        regime, score = "NORMAL", 60

    return {"regime": regime, "confidence": float(score), "reasons": reasons[:3],
            "spread_percent": spread_pct, "order_concentration_percent": concentration}
