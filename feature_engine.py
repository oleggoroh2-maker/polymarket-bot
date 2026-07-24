"""Feature engineering for Polymarket signals.

This module is deliberately dependency-free so it runs reliably on Railway.
The rule score is informative only; it does not block alerts.
"""

from __future__ import annotations

import math
from statistics import mean, pstdev
from typing import Any, Optional

FEATURE_NAMES = [
    "price_logit",
    "log_liquidity",
    "days_left_scaled",
    "base_score",
    "change_5m",
    "change_15m",
    "change_1h",
    "change_24h",
    "momentum_strength",
    "trend_consistency",
    "acceleration",
    "volatility",
]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _change(signal: dict[str, Any], key: str) -> Optional[float]:
    value = signal.get(key)
    if value is None:
        return None
    return _number(value)


def calculate_features(signal: dict[str, Any]) -> dict[str, float]:
    """Return a stable numeric feature vector for rules and future ML."""
    price = _clip(_number(signal.get("price"), 0.5), 0.000001, 0.999999)
    liquidity = max(_number(signal.get("liquidity")), 0.0)
    days_left = max(_number(signal.get("days_left")), 0.0)
    base_score = _clip(_number(signal.get("score")), 0.0, 100.0)

    raw_changes = [
        _change(signal, "change_5m"),
        _change(signal, "change_15m"),
        _change(signal, "change_1h"),
        _change(signal, "change_24h"),
    ]
    known = [value for value in raw_changes if value is not None]
    filled = [value if value is not None else 0.0 for value in raw_changes]

    momentum_strength = max((abs(value) for value in known), default=0.0)
    positive = sum(1 for value in known if value > 0)
    negative = sum(1 for value in known if value < 0)
    trend_consistency = (
        max(positive, negative) / len(known)
        if known else 0.0
    )

    # Positive acceleration means the short-term move is stronger than 1h.
    acceleration = filled[0] - filled[2]
    volatility = pstdev(known) if len(known) >= 2 else 0.0

    return {
        "price_logit": math.log(price / (1.0 - price)),
        "log_liquidity": math.log1p(liquidity),
        "days_left_scaled": math.log1p(days_left),
        "base_score": base_score / 100.0,
        "change_5m": _clip(filled[0] / 100.0, -5.0, 5.0),
        "change_15m": _clip(filled[1] / 100.0, -5.0, 5.0),
        "change_1h": _clip(filled[2] / 100.0, -5.0, 5.0),
        "change_24h": _clip(filled[3] / 100.0, -5.0, 5.0),
        "momentum_strength": _clip(momentum_strength / 100.0, 0.0, 5.0),
        "trend_consistency": trend_consistency,
        "acceleration": _clip(acceleration / 100.0, -5.0, 5.0),
        "volatility": _clip(volatility / 100.0, 0.0, 5.0),
    }


def calculate_rule_assessment(signal: dict[str, Any]) -> dict[str, Any]:
    """Produce a transparent 0-100 quality/risk estimate.

    This is a deterministic baseline, not a trained probability.
    """
    features = calculate_features(signal)
    price = _number(signal.get("price"))
    liquidity = _number(signal.get("liquidity"))
    known_changes = [
        value for value in (
            _change(signal, "change_5m"),
            _change(signal, "change_15m"),
            _change(signal, "change_1h"),
            _change(signal, "change_24h"),
        ) if value is not None
    ]

    liquidity_score = _clip((math.log10(max(liquidity, 10.0)) - 1.0) * 18.0, 0.0, 100.0)
    consistency_score = features["trend_consistency"] * 100.0
    base_score = features["base_score"] * 100.0
    history_score = min(len(known_changes) / 4.0, 1.0) * 100.0

    volatility_penalty = _clip(features["volatility"] * 35.0, 0.0, 45.0)
    micro_price_penalty = 12.0 if price <= 0.003 else 0.0
    illiquid_penalty = 20.0 if liquidity < 1_000 else 0.0

    quality = (
        0.35 * base_score
        + 0.30 * liquidity_score
        + 0.20 * consistency_score
        + 0.15 * history_score
        - volatility_penalty
        - micro_price_penalty
        - illiquid_penalty
    )
    quality = int(round(_clip(quality, 0.0, 100.0)))

    risk = int(round(_clip(
        100.0
        - 0.45 * liquidity_score
        - 0.25 * history_score
        - 0.15 * consistency_score
        + volatility_penalty
        + micro_price_penalty
        + illiquid_penalty,
        0.0,
        100.0,
    )))

    reasons: list[str] = []
    if liquidity >= 500_000:
        reasons.append("высокая ликвидность")
    elif liquidity < 10_000:
        reasons.append("низкая ликвидность")
    if features["trend_consistency"] >= 0.75 and known_changes:
        reasons.append("движение подтверждается периодами")
    if features["volatility"] >= 0.8:
        reasons.append("повышенная волатильность")
    if len(known_changes) < 2:
        reasons.append("мало истории")

    return {
        "ai_quality": quality,
        "ai_risk": risk,
        "feature_count": len(FEATURE_NAMES),
        "reasons": reasons,
        "features": features,
    }


def vector_from_features(features: dict[str, float]) -> list[float]:
    return [_number(features.get(name)) for name in FEATURE_NAMES]
