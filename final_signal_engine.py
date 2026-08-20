"""Final Signal Engine v1.

Combines the bot's already-available evidence into one stable 0..100 quality
score.  V1 is LIVE for enrichment/ranking/visibility but deliberately does NOT
block Telegram delivery; Quality Engine v3 remains the delivery gate.
"""
from __future__ import annotations

import math
from typing import Any


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _optional(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _percent(value: Any) -> float | None:
    number = _optional(value)
    if number is None:
        return None
    if 0.0 <= number <= 1.0:
        number *= 100.0
    return _clip(number)


def _category(alert: dict[str, Any]) -> str:
    text = str(alert.get("category") or "OTHER").upper()
    if "CRYPTO" in text or "BITCOIN" in text:
        return "CRYPTO"
    if "AI" in text or "TECH" in text:
        return "AI/TECH"
    if "SPORT" in text:
        return "SPORTS"
    if "POLIT" in text:
        return "POLITICS"
    if "ETF" in text:
        return "ETF"
    return "OTHER"


def calculate_final_signal(alert: dict[str, Any]) -> dict[str, Any]:
    """Return the final explainable score without using future outcomes."""
    score = _clip(_num(alert.get("score"), 50.0))
    quality = _clip(_num(alert.get("ai_quality"), 50.0))
    risk = _clip(_num(alert.get("ai_risk"), 50.0))
    ml = _percent(alert.get("ml_probability"))
    similarity = _optional(alert.get("similarity_average"))
    if similarity is not None and 0 <= similarity <= 1:
        similarity *= 100.0
    confidence = _optional(alert.get("recalibrated_confidence"))
    if confidence is None:
        confidence = _optional(alert.get("signal_confidence"))
    price = _num(alert.get("current_price", alert.get("price")))
    liquidity = max(0.0, _num(alert.get("liquidity")))
    liq_change = _optional(alert.get("liquidity_change_percent"))
    volume_change = _optional(alert.get("volume_change_percent"))
    category = _category(alert)
    alert_type = str(alert.get("alert_type") or "").upper()

    components: list[dict[str, Any]] = []

    def add(key: str, label: str, points: float, value: Any) -> None:
        components.append({
            "key": key,
            "label": label,
            "points": round(float(points), 2),
            "value": value,
        })

    # Start neutral. Weights follow the stable ordering seen by Feature
    # Intelligence, while the non-monotonic Score behaviour is encoded directly.
    final = 50.0

    if score < 40:
        score_points = 2.0
    elif score < 60:
        score_points = 1.0
    elif score < 75:
        score_points = 8.0
    elif score < 85:
        score_points = -3.0
    else:
        score_points = -15.0
    add("score", "Score calibration", score_points, score)

    add("ai_quality", "AI Quality", (quality - 50.0) * 0.12, quality)
    add("ai_risk", "AI Risk", (50.0 - risk) * 0.11, risk)

    if ml is not None:
        # ML is useful but historically non-monotonic; keep its influence modest.
        ml_points = 3.0 if 25 <= ml < 40 else 1.5 if 10 <= ml < 25 else 0.0
        add("ml", "ML", ml_points, ml)

    if confidence is not None:
        add("confidence", "Confidence", (confidence - 50.0) * 0.04, confidence)

    if similarity is not None:
        sim_points = 3.0 if 80 <= similarity < 90 else 1.5 if 70 <= similarity < 80 else 0.0
        add("similarity", "Similarity", sim_points, similarity)

    if 0.01 <= price < 0.05:
        add("price", "Price 1–5¢", 4.0, price)
    elif 0.20 <= price < 0.50:
        add("price", "Price 20–50¢", 2.0, price)
    elif price >= 0.50:
        add("price", "Price ≥50¢", 1.0, price)
    elif 0 < price < 0.01:
        add("price", "Price <1¢", -3.0, price)

    if 10_000 <= liquidity < 50_000:
        add("liquidity", "Liquidity $10–50k", 3.0, liquidity)
    elif liquidity >= 1_000_000:
        add("liquidity", "Liquidity $1M+", -2.0, liquidity)

    if liq_change is not None and liq_change >= 30:
        add("liquidity_change", "Liquidity Δ 30%+", 4.0, liq_change)
    if volume_change is not None:
        if 20 <= volume_change < 80:
            add("volume_change", "Volume Δ 20–80%", 3.0, volume_change)
        elif volume_change >= 80:
            add("volume_change", "Volume Δ 80%+", 4.0, volume_change)

    category_points = {
        "CRYPTO": 7.0,
        "AI/TECH": 5.0,
        "OTHER": 1.0,
        "SPORTS": 0.0,
        "ETF": 0.0,
        "POLITICS": -8.0,
    }.get(category, 0.0)
    add("category", f"Category {category}", category_points, category)

    if "OPPORTUNITY" in alert_type:
        add("opportunity", "OPPORTUNITY", -18.0, True)

    # Confirmed interaction bonuses. These are intentionally capped so a small
    # historical subgroup cannot dominate the final score.
    if category == "AI/TECH" and 60 <= score < 75:
        confirmations = 0
        confirmations += int(0.01 <= price < 0.05)
        confirmations += int(similarity is not None and 80 <= similarity < 90)
        confirmations += int(liq_change is not None and liq_change >= 30)
        if confirmations:
            add("combo_ai", "AI/TECH confirmed setup", min(10.0, 5.0 + 2.0 * confirmations), confirmations)

    if category == "CRYPTO":
        if volume_change is not None and volume_change >= 80:
            add("combo_crypto_volume", "CRYPTO + volume surge", 8.0, volume_change)
        if similarity is not None and 70 <= similarity < 80:
            add("combo_crypto_similarity", "CRYPTO + similarity 70–79%", 6.0, similarity)

    # Known toxic context from a large, stable sample.
    if score >= 85 and liquidity >= 1_000_000:
        add("toxic_high_score_liq", "85+ + $1M liquidity", -12.0, True)

    final += sum(float(item["points"]) for item in components)
    final = round(_clip(final), 1)

    if final >= 75:
        tier, label = "ELITE", "🔥 ELITE"
    elif final >= 65:
        tier, label = "STRONG", "🟢 STRONG"
    elif final >= 55:
        tier, label = "GOOD", "🟡 GOOD"
    elif final >= 45:
        tier, label = "WATCH", "⚪ WATCH"
    else:
        tier, label = "WEAK", "🔴 WEAK"

    ranked = sorted(components, key=lambda item: abs(float(item["points"])), reverse=True)
    return {
        "final_signal_score": final,
        "final_signal_tier": tier,
        "final_signal_label": label,
        "final_signal_components": ranked,
        "final_signal_version": "v1",
        "final_signal_live": True,
        "final_signal_blocks_delivery": False,
    }


def enrich_with_final_signal(alert: dict[str, Any]) -> dict[str, Any]:
    try:
        return {**alert, **calculate_final_signal(alert)}
    except Exception:
        return {
            **alert,
            "final_signal_score": 50.0,
            "final_signal_tier": "WATCH",
            "final_signal_label": "⚪ WATCH",
            "final_signal_components": [],
            "final_signal_version": "v1-error",
            "final_signal_live": True,
            "final_signal_blocks_delivery": False,
        }
