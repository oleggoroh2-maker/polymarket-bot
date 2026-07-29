"""AI Memory Foundation: outcome classification and aggregate statistics."""

from __future__ import annotations

from contextlib import closing
from typing import Any, Optional

import config
from database import get_connection

SUCCESS_MOVE_PERCENT = float(getattr(config, "AI_SUCCESS_MOVE_PERCENT", 20.0))
PARTIAL_MOVE_PERCENT = float(getattr(config, "AI_PARTIAL_MOVE_PERCENT", 5.0))


def classify_outcome(directional_return_percent: float) -> str:
    """Classify movement in the expected signal direction."""
    value = float(directional_return_percent)
    if value >= SUCCESS_MOVE_PERCENT:
        return "SUCCESS"
    if value >= PARTIAL_MOVE_PERCENT:
        return "PARTIAL"
    return "FAIL"


def get_memory_stats(checkpoint_minutes: int = 1440) -> dict[str, Any]:
    """Return aggregate memory statistics for completed checkpoints."""
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT
                COUNT(*),
                SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'PARTIAL' THEN 1 ELSE 0 END),
                SUM(CASE WHEN status = 'FAIL' THEN 1 ELSE 0 END),
                AVG(directional_return_percent)
            FROM signal_outcomes
            WHERE checkpoint_minutes = ?
              AND status IS NOT NULL
            """,
            (int(checkpoint_minutes),),
        ).fetchone()

    total = int(row[0] or 0) if row else 0
    successful = int(row[1] or 0) if row else 0
    partial = int(row[2] or 0) if row else 0
    failed = int(row[3] or 0) if row else 0
    average = float(row[4] or 0.0) if row else 0.0

    return {
        "checkpoint_minutes": int(checkpoint_minutes),
        "total": total,
        "successful": successful,
        "partial": partial,
        "failed": failed,
        "success_rate": (successful / total * 100.0) if total else None,
        "average_directional_return": average if total else None,
    }


def get_signal_memory(signal_id: str) -> list[dict[str, Any]]:
    """Return all measured checkpoints for one signal."""
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
