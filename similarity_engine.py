"""Find historically similar AI signals without changing live signal scoring.

Similarity Engine v2.1 works in observation mode: it reports historical analogs,
but does not block alerts or modify calibration/ML scores.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import closing
from typing import Any, Optional

import config
from database import get_connection

CHECKPOINT_MINUTES = int(getattr(config, "SIMILARITY_CHECKPOINT_MINUTES", 1440))
MAX_CANDIDATES = int(getattr(config, "SIMILARITY_MAX_CANDIDATES", 1500))
MAX_NEIGHBORS = int(getattr(config, "SIMILARITY_MAX_NEIGHBORS", 80))
MIN_SCORE = float(getattr(config, "SIMILARITY_MIN_SCORE", 60.0))
MIN_SAMPLES = int(getattr(config, "SIMILARITY_MIN_SAMPLES", 8))
CACHE_SECONDS = int(getattr(config, "SIMILARITY_CACHE_SECONDS", 60))

_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])


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


def _direction(alert_type: Any, momentum: Any, change: Any = None) -> str:
    combined = f"{alert_type or ''} {momentum or ''}".upper()
    if any(token in combined for token in ("DIP", "BEAR", "DROP", "FALL")):
        return "DIP"
    if any(token in combined for token in ("PUMP", "BULL", "GROWTH", "RISE")):
        return "PUMP"
    return "DIP" if _num(change) < 0 else "PUMP"


def _category(value: Any) -> str:
    text = str(value or "OTHER").upper().strip()
    aliases = {
        "POLITICS": "POLITICS",
        "CRYPTO": "CRYPTO",
        "SPORTS": "SPORTS",
        "ENTERTAINMENT": "ENTERTAINMENT",
        "CULTURE": "ENTERTAINMENT",
        "CELEBRITY": "ENTERTAINMENT",
        "AI/TECH": "AI/TECH",
        "TECH": "AI/TECH",
    }
    for token, normalized in aliases.items():
        if token in text:
            return normalized
    return text or "OTHER"


def _timeframe_minutes(value: Any) -> int:
    text = str(value or "").lower()
    if "5" in text and ("мин" in text or "5m" in text):
        return 5
    if "15" in text and ("мин" in text or "15m" in text):
        return 15
    if "24" in text and ("час" in text or "24h" in text):
        return 1440
    if "1" in text and ("час" in text or "1h" in text):
        return 60
    return 0


def _feature_vector(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "direction": _direction(
            signal.get("alert_type"),
            signal.get("momentum"),
            signal.get("change_percent"),
        ),
        "category": _category(signal.get("category")),
        "timeframe": _timeframe_minutes(signal.get("timeframe")),
        "score": _num(signal.get("score")),
        "ai_quality": _num(signal.get("ai_quality")),
        "ai_risk": _num(signal.get("ai_risk"), 100.0),
        "ml": _probability_percent(signal.get("ml_probability")),
        "price_change": abs(_num(signal.get("change_percent"))),
        "volume_change": abs(_num(signal.get("volume_change_percent"))),
        "liquidity_change": abs(_num(signal.get("liquidity_change_percent"))),
        "liquidity": max(0.0, _num(signal.get("liquidity"))),
        "days_left": max(0.0, _num(signal.get("days_left"))),
    }


def _load_history() -> list[dict[str, Any]]:
    global _CACHE
    now = time.monotonic()
    cached_at, cached_rows = _CACHE
    if cached_rows and now - cached_at < CACHE_SECONDS:
        return cached_rows

    try:
        with closing(get_connection()) as connection:
            rows = connection.execute(
                """
                SELECT s.signal_id, s.title, s.alert_type, s.category,
                       s.base_score, s.ai_quality, s.ai_risk, s.ml_probability,
                       s.liquidity, s.days_left, s.metadata_json,
                       o.directional_return_percent, o.status
                FROM ai_signals s
                JOIN signal_outcomes o ON o.signal_id = s.signal_id
                WHERE o.checkpoint_minutes = ?
                  AND o.status IS NOT NULL
                ORDER BY o.measured_at DESC
                LIMIT ?
                """,
                (CHECKPOINT_MINUTES, MAX_CANDIDATES),
            ).fetchall()
    except sqlite3.OperationalError:
        # The AI schema may not exist during the very first startup.
        return []

    history: list[dict[str, Any]] = []
    for row in rows:
        try:
            metadata = json.loads(row[10] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        history.append({
            "signal_id": str(row[0]),
            "title": str(row[1]),
            "alert_type": str(row[2]),
            "category": row[3],
            "score": _num(row[4]),
            "ai_quality": _num(row[5]),
            "ai_risk": _num(row[6], 100.0),
            "ml_probability": _probability_percent(row[7]),
            "liquidity": _num(row[8]),
            "days_left": _num(row[9]),
            "timeframe": metadata.get("timeframe"),
            "change_percent": metadata.get("price_change_percent"),
            "volume_change_percent": metadata.get("volume_change_percent"),
            "liquidity_change_percent": metadata.get("liquidity_change_percent"),
            "momentum": metadata.get("momentum"),
            "directional_return": _num(row[11]),
            "status": str(row[12] or ""),
        })

    _CACHE = (now, history)
    return history


def _scaled_similarity(a: float, b: float, scale: float) -> float:
    return max(0.0, 1.0 - min(1.0, abs(a - b) / scale))


def _log_similarity(a: float, b: float) -> float:
    if a <= 0 and b <= 0:
        return 1.0
    la = math.log10(max(1.0, a))
    lb = math.log10(max(1.0, b))
    return max(0.0, 1.0 - min(1.0, abs(la - lb) / 2.0))


def similarity_score(current: dict[str, Any], historical: dict[str, Any]) -> float:
    """Return a 0..100 weighted similarity score."""
    a = _feature_vector(current)
    b = _feature_vector(historical)

    # Same direction is mandatory; opposite moves are not useful analogs.
    if a["direction"] != b["direction"]:
        return 0.0

    components = [
        (0.15, _scaled_similarity(a["price_change"], b["price_change"], 80.0)),
        (0.11, _scaled_similarity(a["volume_change"], b["volume_change"], 250.0)),
        (0.09, _scaled_similarity(a["liquidity_change"], b["liquidity_change"], 100.0)),
        (0.12, _scaled_similarity(a["score"], b["score"], 50.0)),
        (0.12, _scaled_similarity(a["ai_quality"], b["ai_quality"], 50.0)),
        (0.10, _scaled_similarity(a["ai_risk"], b["ai_risk"], 50.0)),
        (0.10, _scaled_similarity(a["ml"], b["ml"], 50.0)),
        (0.08, _log_similarity(a["liquidity"], b["liquidity"])),
        (0.04, _log_similarity(a["days_left"], b["days_left"])),
        (0.05, 1.0 if a["category"] == b["category"] else 0.25),
        (
            0.04,
            1.0
            if a["timeframe"] and a["timeframe"] == b["timeframe"]
            else (0.5 if not a["timeframe"] or not b["timeframe"] else 0.0),
        ),
    ]
    return round(sum(weight * value for weight, value in components) * 100.0, 1)


def analyze_similarity(signal: dict[str, Any]) -> dict[str, Any]:
    """Return historical analog statistics for a live signal."""
    ranked: list[tuple[float, dict[str, Any]]] = []
    for historical in _load_history():
        score = similarity_score(signal, historical)
        if score >= MIN_SCORE:
            ranked.append((score, historical))

    ranked.sort(key=lambda item: item[0], reverse=True)
    selected = ranked[:MAX_NEIGHBORS]
    if len(selected) < MIN_SAMPLES:
        return {
            "similarity_samples": len(selected),
            "similarity_average": None,
            "similarity_strong_rate": None,
            "similarity_continuation_rate": None,
            "similarity_average_return": None,
            "similarity_best_title": None,
            "similarity_best_return": None,
            "similarity_best_score": None,
            "similarity_ready": False,
        }

    rows = [row for _, row in selected]
    scores = [score for score, _ in selected]
    strong = sum(row["status"] == "SUCCESS" for row in rows)
    continued = sum(row["status"] in {"SUCCESS", "PARTIAL"} for row in rows)
    average_return = sum(row["directional_return"] for row in rows) / len(rows)
    best_score, best = max(
        selected,
        key=lambda item: item[1]["directional_return"],
    )

    return {
        "similarity_samples": len(rows),
        "similarity_average": round(sum(scores) / len(scores), 1),
        "similarity_strong_rate": round(strong / len(rows) * 100.0, 1),
        "similarity_continuation_rate": round(continued / len(rows) * 100.0, 1),
        "similarity_average_return": round(average_return, 1),
        "similarity_best_title": best["title"],
        "similarity_best_return": round(best["directional_return"], 1),
        "similarity_best_score": round(best_score, 1),
        "similarity_ready": True,
    }
