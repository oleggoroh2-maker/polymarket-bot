"""Feature Intelligence Engine v2.2 (diagnostic / shadow mode).

Combines the existing bucket-based AI Insights with reliability, historical
stability and day-over-day trend. It does not change live alerts directly.
Adaptive AI may use the reliability-adjusted importance only for shadow weight
proposals.
"""

from __future__ import annotations

import json
import math
from contextlib import closing
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Any

import config
from database import get_connection
from feature_engine import get_feature_importance_report
from price_intelligence import get_price_intelligence_report


LABELS = {
    "score": "Score",
    "ai_quality": "AI Quality",
    "ai_risk": "AI Risk",
    "ml": "ML",
    "liquidity": "Ликвидность",
    "price_change": "Движение цены",
    "volume_change": "Изм. объёма",
    "liquidity_change": "Изм. ликвидности",
    "similarity": "Similarity",
    "price_bucket": "Price Bucket",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _reliability_weight(samples: int) -> float:
    # Smooth support curve: 30 samples ~= 0.32, 150 ~= 0.70, 300 ~= 0.82.
    count = max(0, int(samples))
    return count / (count + 65.0) if count else 0.0


def _stability_label(score: float) -> str:
    if score >= 85:
        return "очень стабильный"
    if score >= 70:
        return "стабильный"
    if score >= 50:
        return "умеренно стабильный"
    if score >= 30:
        return "нестабильный"
    return "предварительный"


def _stars(score: float) -> str:
    count = max(1, min(5, int(round(_clip(score, 0, 100) / 20.0))))
    return "★" * count + "☆" * (5 - count)


def ensure_feature_intelligence_schema() -> None:
    with closing(get_connection()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS feature_intelligence_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                snapshot_date TEXT NOT NULL,
                checkpoint_minutes INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                factors_json TEXT NOT NULL,
                UNIQUE(snapshot_date, checkpoint_minutes)
            );
            CREATE INDEX IF NOT EXISTS idx_feature_intelligence_date
            ON feature_intelligence_snapshots(snapshot_date, checkpoint_minutes);
            """
        )
        connection.commit()


def _price_factor(price_report: dict[str, Any]) -> dict[str, Any] | None:
    buckets = list(price_report.get("buckets") or [])
    eligible = [item for item in buckets if int(item.get("samples") or 0) > 0]
    if len(eligible) < 2:
        return None
    rates = [_safe_float(item.get("continuation_rate")) for item in eligible]
    best = max(eligible, key=lambda item: (_safe_float(item.get("continuation_rate")), _safe_float(item.get("normalized_average_return"))))
    worst = min(eligible, key=lambda item: (_safe_float(item.get("continuation_rate")), _safe_float(item.get("normalized_average_return"))))
    total = sum(int(item.get("samples") or 0) for item in eligible)
    importance = (max(rates) - min(rates)) * min(1.0, total / 500.0)
    return {
        "key": "price_bucket",
        "label": LABELS["price_bucket"],
        "importance": round(importance, 2),
        "best": {
            "label": str(best.get("label") or "—"),
            "samples": int(best.get("samples") or 0),
            "continuation_rate": _safe_float(best.get("continuation_rate")),
            "average_return": _safe_float(best.get("normalized_average_return")),
        },
        "worst": {
            "label": str(worst.get("label") or "—"),
            "samples": int(worst.get("samples") or 0),
            "continuation_rate": _safe_float(worst.get("continuation_rate")),
            "average_return": _safe_float(worst.get("normalized_average_return")),
        },
    }


def _history(limit: int = 14) -> list[dict[str, Any]]:
    ensure_feature_intelligence_schema()
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT created_at, sample_count, factors_json
            FROM feature_intelligence_snapshots
            ORDER BY snapshot_date DESC, id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    output: list[dict[str, Any]] = []
    for created_at, sample_count, raw in rows:
        try:
            factors = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            factors = {}
        output.append({
            "created_at": str(created_at),
            "sample_count": int(sample_count or 0),
            "factors": factors if isinstance(factors, dict) else {},
        })
    return output


def _trend_and_stability(key: str, current: float, history: list[dict[str, Any]], support: int) -> tuple[float, float]:
    previous_values: list[float] = []
    for snapshot in history:
        value = (snapshot.get("factors") or {}).get(key)
        if value is not None:
            previous_values.append(_safe_float(value))
    trend = current - previous_values[0] if previous_values else 0.0
    series = [current] + previous_values[:6]
    variation = pstdev(series) if len(series) >= 2 else 0.0
    support_score = min(100.0, support / 3.0)
    variation_penalty = min(70.0, variation * 8.0)
    stability = _clip(25.0 + support_score * 0.65 - variation_penalty, 0.0, 100.0)
    return round(trend, 2), round(stability, 1)


def get_feature_intelligence_report(
    checkpoint_minutes: int | None = None,
    max_rows: int | None = None,
    min_bucket_samples: int | None = None,
    save: bool = True,
) -> dict[str, Any]:
    ensure_feature_intelligence_schema()
    checkpoint = int(checkpoint_minutes or getattr(config, "FEATURE_INTELLIGENCE_CHECKPOINT_MINUTES", 1440))
    limit = int(max_rows or getattr(config, "FEATURE_INTELLIGENCE_MAX_ROWS", 5000))
    minimum = int(min_bucket_samples or getattr(config, "FEATURE_INTELLIGENCE_MIN_BUCKET_SAMPLES", 20))

    base = get_feature_importance_report(checkpoint, limit, minimum)
    price = get_price_intelligence_report(checkpoint, limit)
    factors = [dict(item) for item in (base.get("factors") or [])]
    price_item = _price_factor(price)
    if price_item:
        factors.append(price_item)

    history = _history(int(getattr(config, "FEATURE_INTELLIGENCE_HISTORY_DAYS", 14)))
    prepared: list[dict[str, Any]] = []
    for factor in factors:
        key = str(factor.get("key") or "")
        if not key:
            continue
        best = dict(factor.get("best") or {})
        samples = int(best.get("samples") or 0)
        raw_importance = _safe_float(factor.get("importance"))
        support = _reliability_weight(samples)
        effective = raw_importance * (0.45 + 0.55 * support)
        trend, stability = _trend_and_stability(key, effective, history, samples)
        prepared.append({
            **factor,
            "label": str(factor.get("label") or LABELS.get(key, key)),
            "raw_importance": round(raw_importance, 2),
            "effective_importance": round(effective, 2),
            "trend": trend,
            "stability": stability,
            "stability_label": _stability_label(stability),
            "stability_stars": _stars(stability),
            "support_weight": round(support, 3),
        })

    prepared.sort(key=lambda item: item["effective_importance"], reverse=True)
    total_effective = sum(max(0.0, item["effective_importance"]) for item in prepared)
    for item in prepared:
        item["share"] = (
            item["effective_importance"] / total_effective * 100.0
            if total_effective > 0 else 0.0
        )

    result = {
        "created_at": _now_iso(),
        "checkpoint_minutes": checkpoint,
        "sample_count": int(base.get("total") or 0),
        "factors": prepared,
        "shadow_mode": True,
    }

    if save and result["sample_count"] > 0:
        snapshot = {item["key"]: round(item["effective_importance"], 3) for item in prepared}
        with closing(get_connection()) as connection:
            connection.execute(
                """
                INSERT INTO feature_intelligence_snapshots (
                    created_at, snapshot_date, checkpoint_minutes,
                    sample_count, factors_json
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_date, checkpoint_minutes) DO UPDATE SET
                    created_at=excluded.created_at,
                    sample_count=excluded.sample_count,
                    factors_json=excluded.factors_json
                """,
                (
                    result["created_at"], result["created_at"][:10], checkpoint,
                    result["sample_count"], json.dumps(snapshot, ensure_ascii=False),
                ),
            )
            connection.commit()
    return result


def format_feature_intelligence_report(report: dict[str, Any]) -> str:
    total = int(report.get("sample_count") or 0)
    if total <= 0:
        return "📊 Feature Intelligence · Shadow Mode\n\nПока нет проверенных сигналов."

    lines = [
        "📊 Feature Intelligence · Shadow Mode",
        "",
        f"Проверено сигналов: {total}",
        "⚠️ Модуль пока не меняет реальные алерты.",
        "",
        "🧠 Реальная значимость факторов",
    ]
    for index, item in enumerate((report.get("factors") or [])[:9], start=1):
        trend = float(item.get("trend") or 0.0)
        arrow = "▲" if trend > 0.15 else "▼" if trend < -0.15 else "→"
        best = item.get("best") or {}
        lines.extend([
            f"{index}. {item['label']}: {float(item.get('share') or 0):.1f}% "
            f"({float(item.get('effective_importance') or 0):.1f}/100)",
            f"   {arrow} тренд {trend:+.1f} · {item.get('stability_stars')} "
            f"{item.get('stability_label')}",
            f"   Лучший диапазон: {best.get('label', '—')} · n={int(best.get('samples') or 0)}",
        ])

    lines.extend([
        "",
        "ℹ️ Значимость учитывает разницу результатов между диапазонами, размер выборки и стабильность по дням.",
        "Adaptive AI использует эти данные только для теневых рекомендаций.",
    ])
    return "\n".join(lines)
