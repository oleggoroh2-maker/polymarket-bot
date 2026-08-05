"""AI Memory: outcome classification, audit and aggregate statistics."""

from __future__ import annotations

from contextlib import closing
from statistics import median, pstdev
from typing import Any

import config
from database import get_connection
from result_normalization import (
    capped_return_percent,
    entry_price_bucket,
    normalized_training_return,
)

SUCCESS_MOVE_PERCENT = float(getattr(config, "AI_SUCCESS_MOVE_PERCENT", 10.0))
PARTIAL_MOVE_PERCENT = float(getattr(config, "AI_PARTIAL_MOVE_PERCENT", 3.0))
NEUTRAL_MOVE_PERCENT = float(getattr(config, "AI_NEUTRAL_MOVE_PERCENT", 3.0))


def classify_outcome(directional_return_percent: float) -> str:
    """Classify the signed return relative to the expected signal direction."""
    value = float(directional_return_percent)
    if value >= SUCCESS_MOVE_PERCENT:
        return "SUCCESS"
    if value >= PARTIAL_MOVE_PERCENT:
        return "PARTIAL"
    if value > -NEUTRAL_MOVE_PERCENT:
        return "NEUTRAL"
    return "FAIL"


def _trimmed_mean(values: list[float], trim_fraction: float = 0.05) -> float | None:
    """Return a symmetric trimmed mean, or the ordinary mean for small samples."""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    trim = int(len(ordered) * max(0.0, min(float(trim_fraction), 0.20)))
    if trim > 0 and len(ordered) - 2 * trim >= 1:
        ordered = ordered[trim:-trim]
    return sum(ordered) / len(ordered)


def _result_distribution(values: list[float]) -> dict[str, int]:
    distribution = {
        "gte_50": 0,
        "20_to_50": 0,
        "0_to_20": 0,
        "zero": 0,
        "minus_20_to_0": 0,
        "minus_50_to_minus_20": 0,
        "lt_minus_50": 0,
    }
    for value in values:
        number = float(value)
        if number >= 50:
            distribution["gte_50"] += 1
        elif number >= 20:
            distribution["20_to_50"] += 1
        elif number > 0:
            distribution["0_to_20"] += 1
        elif abs(number) < 1e-12:
            distribution["zero"] += 1
        elif number > -20:
            distribution["minus_20_to_0"] += 1
        elif number > -50:
            distribution["minus_50_to_minus_20"] += 1
        else:
            distribution["lt_minus_50"] += 1
    return distribution


def get_memory_stats(checkpoint_minutes: int = 1440) -> dict[str, Any]:
    """Return aggregate, robust-return and direction-split checkpoint statistics."""
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN o.status = 'SUCCESS' THEN 1 ELSE 0 END),
                SUM(CASE WHEN o.status = 'PARTIAL' THEN 1 ELSE 0 END),
                SUM(CASE WHEN o.status = 'NEUTRAL' THEN 1 ELSE 0 END),
                SUM(CASE WHEN o.status = 'FAIL' THEN 1 ELSE 0 END),
                AVG(o.directional_return_percent),
                SUM(CASE WHEN UPPER(s.alert_type) LIKE '%PUMP%' THEN 1 ELSE 0 END),
                SUM(CASE WHEN UPPER(s.alert_type) LIKE '%PUMP%' AND o.status = 'SUCCESS' THEN 1 ELSE 0 END),
                SUM(CASE WHEN UPPER(s.alert_type) LIKE '%DIP%' THEN 1 ELSE 0 END),
                SUM(CASE WHEN UPPER(s.alert_type) LIKE '%DIP%' AND o.status = 'SUCCESS' THEN 1 ELSE 0 END)
            FROM signal_outcomes o
            JOIN ai_signals s ON s.signal_id = o.signal_id
            WHERE o.checkpoint_minutes = ?
              AND o.status IS NOT NULL
            """,
            (int(checkpoint_minutes),),
        ).fetchone()
        return_rows = connection.execute(
            """
            SELECT o.directional_return_percent, s.entry_price, o.status
            FROM signal_outcomes o
            JOIN ai_signals s ON s.signal_id = o.signal_id
            WHERE o.checkpoint_minutes = ?
              AND o.status IS NOT NULL
              AND o.directional_return_percent IS NOT NULL
            """,
            (int(checkpoint_minutes),),
        ).fetchall()

    values = [float(item[0]) for item in return_rows]
    capped_values = [capped_return_percent(value) for value in values]
    normalized_values = [normalized_training_return(value) for value in values]
    price_buckets: dict[str, dict[str, float | int | None]] = {}
    for raw_return, entry_price, status in return_rows:
        label = entry_price_bucket(entry_price)
        bucket = price_buckets.setdefault(label, {
            "samples": 0, "strong": 0, "continued": 0,
            "normalized_sum": 0.0, "raw_sum": 0.0,
        })
        bucket["samples"] = int(bucket["samples"]) + 1
        bucket["strong"] = int(bucket["strong"]) + (1 if str(status).upper() == "SUCCESS" else 0)
        bucket["continued"] = int(bucket["continued"]) + (1 if str(status).upper() in {"SUCCESS", "PARTIAL"} else 0)
        bucket["normalized_sum"] = float(bucket["normalized_sum"]) + normalized_training_return(raw_return)
        bucket["raw_sum"] = float(bucket["raw_sum"]) + float(raw_return)
    price_bucket_stats = []
    for label in ("<1¢", "1–5¢", "5–20¢", "20–50¢", "≥50¢"):
        bucket = price_buckets.get(label)
        if not bucket:
            continue
        samples = int(bucket["samples"])
        price_bucket_stats.append({
            "label": label,
            "samples": samples,
            "strong_rate": int(bucket["strong"]) / samples * 100.0,
            "continuation_rate": int(bucket["continued"]) / samples * 100.0,
            "normalized_average_return": float(bucket["normalized_sum"]) / samples,
            "raw_average_return": float(bucket["raw_sum"]) / samples,
        })
    total = int(row[0] or 0) if row else 0
    successful = int(row[1] or 0) if row else 0
    partial = int(row[2] or 0) if row else 0
    neutral = int(row[3] or 0) if row else 0
    failed = int(row[4] or 0) if row else 0
    average = float(row[5] or 0.0) if row else 0.0
    pump_total = int(row[6] or 0) if row else 0
    pump_successful = int(row[7] or 0) if row else 0
    dip_total = int(row[8] or 0) if row else 0
    dip_successful = int(row[9] or 0) if row else 0
    continued = successful + partial

    robust_median = median(values) if values else None
    trimmed = _trimmed_mean(values, 0.05)
    mean_absolute = (sum(abs(value) for value in values) / len(values)) if values else None
    standard_deviation = pstdev(values) if len(values) >= 2 else (0.0 if values else None)
    capped_average = (sum(capped_values) / len(capped_values)) if capped_values else None
    normalized_average = (sum(normalized_values) / len(normalized_values)) if normalized_values else None
    normalized_median = median(normalized_values) if normalized_values else None
    normalized_stddev = pstdev(normalized_values) if len(normalized_values) >= 2 else (0.0 if normalized_values else None)

    return {
        "checkpoint_minutes": int(checkpoint_minutes),
        "total": total,
        "successful": successful,
        "partial": partial,
        "neutral": neutral,
        "failed": failed,
        "success_rate": (successful / total * 100.0) if total else None,
        "continuation_rate": (continued / total * 100.0) if total else None,
        "average_directional_return": average if total else None,
        "median_directional_return": robust_median,
        "trimmed_mean_directional_return": trimmed,
        "mean_absolute_directional_return": mean_absolute,
        "directional_return_stddev": standard_deviation,
        "capped_average_directional_return": capped_average,
        "normalized_average_directional_return": normalized_average,
        "normalized_median_directional_return": normalized_median,
        "normalized_directional_return_stddev": normalized_stddev,
        "result_distribution": _result_distribution(values),
        "entry_price_buckets": price_bucket_stats,
        "pump_total": pump_total,
        "pump_successful": pump_successful,
        "dip_total": dip_total,
        "dip_successful": dip_successful,
    }


def get_signal_memory(signal_id: str) -> list[dict[str, Any]]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT checkpoint_minutes, measured_at, price,
                   return_percent, directional_return_percent,
                   max_price, min_price, success, status
            FROM signal_outcomes
            WHERE signal_id = ?
            ORDER BY checkpoint_minutes ASC
            """,
            (str(signal_id),),
        ).fetchall()

    return [
        {
            "checkpoint_minutes": int(row[0]),
            "measured_at": row[1],
            "price": float(row[2]),
            "return_percent": float(row[3]),
            "directional_return_percent": float(row[4]),
            "max_price": float(row[5]),
            "min_price": float(row[6]),
            "success": None if row[7] is None else bool(row[7]),
            "status": row[8],
        }
        for row in rows
    ]


def get_recent_memory_audit(checkpoint_minutes: int = 1440, limit: int = 10) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 30))
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT s.signal_id, s.title, s.alert_type, s.alert_label,
                   s.created_at, s.entry_price, o.measured_at, o.price,
                   o.return_percent, o.directional_return_percent, o.status
            FROM signal_outcomes o
            JOIN ai_signals s ON s.signal_id = o.signal_id
            WHERE o.checkpoint_minutes = ? AND o.status IS NOT NULL
            ORDER BY o.measured_at DESC
            LIMIT ?
            """,
            (int(checkpoint_minutes), safe_limit),
        ).fetchall()

    return [
        {
            "signal_id": str(row[0]), "title": str(row[1]),
            "alert_type": str(row[2]), "alert_label": str(row[3]),
            "created_at": row[4], "entry_price": float(row[5]),
            "measured_at": row[6], "measured_price": float(row[7]),
            "return_percent": float(row[8]),
            "directional_return_percent": float(row[9]),
            "status": str(row[10]),
        }
        for row in rows
    ]
