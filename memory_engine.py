"""AI Memory: outcome classification, audit and aggregate statistics."""

from __future__ import annotations

from contextlib import closing
from typing import Any

import config
from database import get_connection

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


def get_memory_stats(checkpoint_minutes: int = 1440) -> dict[str, Any]:
    """Return aggregate and direction-split statistics for a checkpoint."""
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
