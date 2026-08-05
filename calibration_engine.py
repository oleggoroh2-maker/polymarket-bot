"""Historical calibration and per-subscriber signal quality filtering."""

from __future__ import annotations

import json
import math
from contextlib import closing
from typing import Any

import config
from database import get_connection
from result_normalization import normalized_training_return

CHECKPOINT_MINUTES = int(getattr(config, "CALIBRATION_CHECKPOINT_MINUTES", 1440))
MIN_HISTORY_SAMPLES = int(getattr(config, "CALIBRATION_MIN_HISTORY_SAMPLES", 20))
PREMIUM_THRESHOLD = float(getattr(config, "CALIBRATION_PREMIUM_THRESHOLD", 72.0))
GOOD_THRESHOLD = float(getattr(config, "CALIBRATION_GOOD_THRESHOLD", 52.0))


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _probability_percent(value: Any) -> float:
    result = _num(value)
    if 0.0 <= result <= 1.0:
        result *= 100.0
    return max(0.0, min(100.0, result))


def _direction(alert_type: Any, momentum: Any) -> str:
    combined = f"{alert_type or ''} {momentum or ''}".upper()
    if any(token in combined for token in ("DIP", "BEAR", "DROP", "FALL")):
        return "DIP"
    return "PUMP"


def _history_rows() -> list[dict[str, Any]]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT s.alert_type, s.base_score, s.ai_quality, s.ai_risk,
                   s.ml_probability, s.liquidity, s.metadata_json,
                   o.directional_return_percent, o.status
            FROM ai_signals s
            JOIN signal_outcomes o ON o.signal_id = s.signal_id
            WHERE o.checkpoint_minutes = ? AND o.status IS NOT NULL
            """,
            (CHECKPOINT_MINUTES,),
        ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            metadata = json.loads(row[6] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        result.append({
            "direction": _direction(row[0], metadata.get("momentum")),
            "score": _num(row[1]),
            "ai_quality": _num(row[2]),
            "ai_risk": _num(row[3], 100.0),
            "ml": _probability_percent(row[4]),
            "liquidity": _num(row[5]),
            "price_change": abs(_num(metadata.get("price_change_percent"))),
            "volume_change": abs(_num(metadata.get("volume_change_percent"))),
            "liquidity_change": abs(_num(metadata.get("liquidity_change_percent"))),
            "return": normalized_training_return(row[7]),
            "raw_return": _num(row[7]),
            "status": str(row[8] or ""),
        })
    return result


def _similarity_distance(current: dict[str, Any], historical: dict[str, Any]) -> float:
    # Normalized Manhattan distance. Lower means more similar.
    weights = {
        "score": 0.16,
        "ai_quality": 0.18,
        "ai_risk": 0.14,
        "ml": 0.16,
        "price_change": 0.14,
        "volume_change": 0.10,
        "liquidity_change": 0.07,
    }
    scales = {
        "score": 100.0,
        "ai_quality": 100.0,
        "ai_risk": 100.0,
        "ml": 100.0,
        "price_change": 100.0,
        "volume_change": 250.0,
        "liquidity_change": 100.0,
    }
    distance = 0.0
    for key, weight in weights.items():
        distance += weight * min(
            1.0,
            abs(current[key] - historical[key]) / scales[key],
        )
    if current["direction"] != historical["direction"]:
        distance += 0.18
    return distance


def calibrate_signal(signal: dict[str, Any]) -> dict[str, Any]:
    current = {
        "direction": _direction(signal.get("alert_type"), signal.get("momentum")),
        "score": _num(signal.get("score")),
        "ai_quality": _num(signal.get("ai_quality")),
        "ai_risk": _num(signal.get("ai_risk"), 100.0),
        "ml": _probability_percent(signal.get("ml_probability")),
        "price_change": abs(_num(signal.get("change_percent"))),
        "volume_change": abs(_num(signal.get("volume_change_percent"))),
        "liquidity_change": abs(_num(signal.get("liquidity_change_percent"))),
    }

    # Stable prior score. Historical evidence adjusts it, but does not fully replace it.
    prior = (
        current["score"] * 0.20
        + current["ai_quality"] * 0.25
        + (100.0 - current["ai_risk"]) * 0.20
        + current["ml"] * 0.15
        + min(current["price_change"], 100.0) * 0.10
        + min(current["volume_change"], 200.0) / 2.0 * 0.06
        + min(current["liquidity_change"], 100.0) * 0.04
    )

    rows = _history_rows()
    ranked = sorted(
        ((_similarity_distance(current, row), row) for row in rows),
        key=lambda pair: pair[0],
    )
    neighbors = [row for distance, row in ranked[:120] if distance <= 0.42]

    if len(neighbors) >= MIN_HISTORY_SAMPLES:
        strong_rate = sum(row["status"] == "SUCCESS" for row in neighbors) / len(neighbors)
        continuation_rate = sum(
            row["status"] in {"SUCCESS", "PARTIAL"} for row in neighbors
        ) / len(neighbors)
        average_return = sum(row["return"] for row in neighbors) / len(neighbors)
        historical_score = (
            strong_rate * 65.0
            + continuation_rate * 25.0
            + max(-10.0, min(10.0, average_return))
        )
        confidence = prior * 0.55 + historical_score * 0.45
    else:
        strong_rate = None
        continuation_rate = None
        average_return = None
        confidence = prior

    confidence = max(0.0, min(100.0, confidence))
    if confidence >= PREMIUM_THRESHOLD:
        tier = "PREMIUM"
        badge = "⭐ PREMIUM"
    elif confidence >= GOOD_THRESHOLD:
        tier = "GOOD"
        badge = "🟢 GOOD"
    else:
        tier = "WATCH"
        badge = "🟡 WATCH"

    stars_count = max(1, min(5, math.ceil(confidence / 20.0)))
    return {
        "calibration_confidence": round(confidence, 1),
        "calibration_tier": tier,
        "calibration_badge": badge,
        "calibration_stars": "★" * stars_count + "☆" * (5 - stars_count),
        "calibration_samples": len(neighbors),
        "calibration_strong_rate": None if strong_rate is None else strong_rate * 100.0,
        "calibration_continuation_rate": (
            None if continuation_rate is None else continuation_rate * 100.0
        ),
        "calibration_average_return": average_return,
    }


def signal_passes_mode(signal: dict[str, Any], mode: str) -> bool:
    normalized = str(mode or "ALL").upper()
    tier = str(signal.get("calibration_tier") or "WATCH").upper()
    if normalized == "PREMIUM":
        return tier == "PREMIUM"
    if normalized == "GOOD":
        return tier in {"PREMIUM", "GOOD"}
    return True


def get_calibration_report() -> dict[str, Any]:
    rows = _history_rows()
    total = len(rows)
    if not rows:
        return {"total": 0, "strong_rate": None, "continuation_rate": None, "average": None}
    return {
        "total": total,
        "strong_rate": sum(row["status"] == "SUCCESS" for row in rows) / total * 100.0,
        "continuation_rate": sum(
            row["status"] in {"SUCCESS", "PARTIAL"} for row in rows
        ) / total * 100.0,
        "average": sum(row["return"] for row in rows) / total,
        "premium_threshold": PREMIUM_THRESHOLD,
        "good_threshold": GOOD_THRESHOLD,
    }
