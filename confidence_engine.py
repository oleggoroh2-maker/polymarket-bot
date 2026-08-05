"""Signal Confidence Engine v1.0 (shadow mode).

Builds one explainable 0..100 confidence score from the signals already produced
by the bot. The score is stored for later 24-hour validation, but it never blocks
or promotes live alerts in this version.
"""

from __future__ import annotations

import json
import math
from contextlib import closing
from datetime import datetime, timezone
from typing import Any, Optional

import config
from database import get_connection
from result_normalization import normalized_training_return


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _optional_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _probability_percent(value: Any) -> Optional[float]:
    number = _optional_number(value)
    if number is None:
        return None
    if 0.0 <= number <= 1.0:
        number *= 100.0
    return _clip(number)


def _category_adjustment(category: Any) -> float:
    """Small conservative prior; historical reports remain the source of truth."""
    text = str(category or "").upper()
    if "POLIT" in text:
        return -6.0
    if "SPORT" in text:
        return 4.0
    if "CRYPTO" in text or "BITCOIN" in text:
        return 3.0
    if "AI" in text or "TECH" in text:
        return 1.0
    return 0.0


def _tier(score: float) -> str:
    if score >= 85:
        return "ELITE"
    if score >= 72:
        return "PREMIUM"
    if score >= 60:
        return "GOOD"
    if score >= 45:
        return "WATCH"
    return "LOW"


def _tier_label(tier: str) -> str:
    return {
        "ELITE": "🔥 ELITE",
        "PREMIUM": "⭐ PREMIUM",
        "GOOD": "🟢 GOOD",
        "WATCH": "🟡 WATCH",
        "LOW": "⚪ LOW",
    }.get(str(tier).upper(), "⚪ LOW")


def ensure_confidence_schema() -> None:
    with closing(get_connection()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS confidence_signals (
                signal_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                confidence REAL NOT NULL,
                tier TEXT NOT NULL,
                components_json TEXT NOT NULL,
                inputs_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_confidence_created
            ON confidence_signals (created_at DESC);
            """
        )
        connection.commit()


def calculate_confidence(alert: dict[str, Any]) -> dict[str, Any]:
    """Return confidence fields without changing alert delivery behavior."""
    score = _clip(_number(alert.get("score")))
    quality = _clip(_number(alert.get("ai_quality")))
    risk = _clip(_number(alert.get("ai_risk"), 50.0))
    ml = _probability_percent(alert.get("ml_probability"))
    similarity = _optional_number(alert.get("similarity_average"))
    history_rate = _optional_number(alert.get("similarity_strong_rate"))
    if history_rate is None:
        history_rate = _optional_number(alert.get("calibration_strong_rate"))
    liquidity = max(0.0, _number(alert.get("liquidity")))
    volume_change = _optional_number(alert.get("volume_change_percent"))
    liquidity_change = _optional_number(alert.get("liquidity_change_percent"))

    # Base is deliberately neutral. Each module contributes a bounded amount.
    components: list[dict[str, Any]] = []

    def add(key: str, label: str, points: float, value: Any) -> None:
        points = max(-15.0, min(15.0, float(points)))
        components.append({
            "key": key,
            "label": label,
            "points": round(points, 2),
            "value": value,
        })

    add("score", "Score", (score - 50.0) * 0.16, score)
    add("ai_quality", "AI Quality", (quality - 50.0) * 0.18, quality)
    add("ai_risk", "AI Risk", (50.0 - risk) * 0.16, risk)

    if ml is not None:
        add("ml", "ML", (ml - 50.0) * 0.10, ml)
    if similarity is not None:
        add("similarity", "Similarity", (similarity - 60.0) * 0.18, similarity)
    if history_rate is not None:
        # 13% is close to the historical global baseline in the current dataset.
        add("history", "AI Memory", (history_rate - 13.0) * 0.28, history_rate)

    # Log scale prevents million-dollar markets from dominating the result.
    if liquidity > 0:
        liquidity_strength = _clip(20.0 * math.log10(max(1.0, liquidity) / 10_000.0) + 35.0)
        add("liquidity", "Liquidity", (liquidity_strength - 50.0) * 0.10, liquidity)

    if volume_change is not None:
        add("volume_change", "Volume Δ", _clip(volume_change, -50, 100) * 0.045, volume_change)
    if liquidity_change is not None:
        add(
            "liquidity_change",
            "Liquidity Δ",
            _clip(liquidity_change, -50, 100) * 0.065,
            liquidity_change,
        )

    category_points = _category_adjustment(alert.get("category"))
    if category_points:
        add("category", "Category", category_points, str(alert.get("category") or ""))

    confidence = _clip(50.0 + sum(float(item["points"]) for item in components))
    tier = _tier(confidence)
    positive = sorted(
        (item for item in components if float(item["points"]) > 0),
        key=lambda item: float(item["points"]),
        reverse=True,
    )
    negative = sorted(
        (item for item in components if float(item["points"]) < 0),
        key=lambda item: float(item["points"]),
    )

    return {
        "signal_confidence": round(confidence, 1),
        "confidence_tier": tier,
        "confidence_label": _tier_label(tier),
        "confidence_components": components,
        "confidence_positive": positive[:4],
        "confidence_negative": negative[:4],
        "confidence_shadow_mode": True,
    }


def enrich_with_confidence(alert: dict[str, Any]) -> dict[str, Any]:
    try:
        return {**alert, **calculate_confidence(alert)}
    except Exception:
        return {
            **alert,
            "signal_confidence": 50.0,
            "confidence_tier": "WATCH",
            "confidence_label": "🟡 WATCH",
            "confidence_components": [],
            "confidence_shadow_mode": True,
        }


def record_confidence_signal(signal_id: str, alert: dict[str, Any]) -> None:
    ensure_confidence_schema()
    result = calculate_confidence(alert)
    inputs = {
        key: alert.get(key)
        for key in (
            "score", "ai_quality", "ai_risk", "ml_probability", "liquidity",
            "volume_change_percent", "liquidity_change_percent",
            "similarity_average", "similarity_strong_rate",
            "calibration_strong_rate", "category", "alert_type",
        )
    }
    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO confidence_signals (
                signal_id, created_at, confidence, tier,
                components_json, inputs_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(signal_id),
                _now_iso(),
                float(result["signal_confidence"]),
                str(result["confidence_tier"]),
                json.dumps(result["confidence_components"], ensure_ascii=False),
                json.dumps(inputs, ensure_ascii=False),
            ),
        )
        connection.commit()


_BUCKETS = (
    ("85–100", 85.0, 101.0),
    ("72–84", 72.0, 85.0),
    ("60–71", 60.0, 72.0),
    ("45–59", 45.0, 60.0),
    ("<45", 0.0, 45.0),
)


def get_confidence_report(
    checkpoint_minutes: int | None = None,
    max_rows: int | None = None,
) -> dict[str, Any]:
    ensure_confidence_schema()
    checkpoint = int(
        checkpoint_minutes
        if checkpoint_minutes is not None
        else getattr(config, "CONFIDENCE_CHECKPOINT_MINUTES", 1440)
    )
    limit = int(
        max_rows
        if max_rows is not None
        else getattr(config, "CONFIDENCE_MAX_ROWS", 5000)
    )
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT c.confidence, c.tier, o.status,
                   o.directional_return_percent
            FROM confidence_signals c
            JOIN signal_outcomes o ON o.signal_id = c.signal_id
            WHERE o.checkpoint_minutes = ? AND o.status IS NOT NULL
            ORDER BY o.measured_at DESC
            LIMIT ?
            """,
            (checkpoint, max(1, limit)),
        ).fetchall()

    prepared = [
        {
            "confidence": float(row[0]),
            "tier": str(row[1]),
            "status": str(row[2]).upper(),
            "return": normalized_training_return(row[3]),
            "raw_return": float(row[3] or 0.0),
        }
        for row in rows
    ]
    bucket_rows: list[dict[str, Any]] = []
    for label, low, high in _BUCKETS:
        selected = [row for row in prepared if low <= row["confidence"] < high]
        total = len(selected)
        strong = sum(row["status"] == "SUCCESS" for row in selected)
        continuation = sum(row["status"] in {"SUCCESS", "PARTIAL"} for row in selected)
        average = sum(row["return"] for row in selected) / total if total else None
        bucket_rows.append({
            "label": label,
            "total": total,
            "strong": strong,
            "strong_rate": strong / total * 100.0 if total else None,
            "continuation_rate": continuation / total * 100.0 if total else None,
            "average_return": average,
        })

    return {
        "checkpoint_minutes": checkpoint,
        "evaluated": len(prepared),
        "minimum": int(getattr(config, "CONFIDENCE_MIN_EVALUATED", 30)),
        "ready": len(prepared) >= int(getattr(config, "CONFIDENCE_MIN_EVALUATED", 30)),
        "buckets": bucket_rows,
    }


def format_confidence_report(report: dict[str, Any]) -> str:
    evaluated = int(report.get("evaluated") or 0)
    minimum = int(report.get("minimum") or 30)
    lines = [
        "🎯 Signal Confidence · Shadow Mode",
        "",
        f"Проверено новых сигналов: {evaluated}",
        "⚠️ Confidence пока НЕ влияет на отправку алертов.",
    ]
    if evaluated == 0:
        lines.extend([
            "",
            "Пока нет сигналов с Confidence, прошедших 24-часовую проверку.",
            "Первые результаты появятся после полного контрольного периода.",
        ])
        return "\n".join(lines)

    if evaluated < minimum:
        lines.extend(["", f"Для устойчивого сравнения нужно минимум {minimum} сигналов."])

    lines.extend(["", "📊 Результаты по диапазонам"])
    for bucket in report.get("buckets") or []:
        total = int(bucket.get("total") or 0)
        if not total:
            lines.append(f"• {bucket['label']}: пока нет данных")
            continue
        strong = float(bucket.get("strong_rate") or 0.0)
        continuation = float(bucket.get("continuation_rate") or 0.0)
        average = bucket.get("average_return")
        average_text = f"{float(average):+.1f}%" if average is not None else "—"
        lines.extend([
            f"• Confidence {bucket['label']} (n={total})",
            f"  Strong: {strong:.1f}% · Любое: {continuation:.1f}% · Норм.: {average_text}",
        ])

    lines.extend([
        "",
        "Цель Shadow Mode — проверить, растёт ли качество вместе с Confidence.",
    ])
    return "\n".join(lines)
