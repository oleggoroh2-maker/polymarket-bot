"""Price Intelligence Engine v1.0 (shadow mode).

Learns historical effectiveness by entry-price bucket and adds a conservative,
explainable price prior to Confidence and the AI Simulator. It never blocks or
promotes live alerts in this version.
"""

from __future__ import annotations

import math
import sqlite3
import time
from contextlib import closing
from typing import Any, Optional

import config
from database import get_connection
from result_normalization import normalized_training_return


PRICE_BUCKETS = (
    ("<1¢", 0.0, 0.01),
    ("1–5¢", 0.01, 0.05),
    ("5–20¢", 0.05, 0.20),
    ("20–50¢", 0.20, 0.50),
    ("≥50¢", 0.50, float("inf")),
)

_CACHE: dict[str, Any] = {"expires": 0.0, "report": None}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _entry_price(alert: dict[str, Any]) -> float:
    value = alert.get("current_price")
    if value is None:
        value = alert.get("price")
    return max(0.0, _number(value))


def price_bucket(price: Any) -> tuple[str, float, float]:
    value = max(0.0, _number(price))
    for label, low, high in PRICE_BUCKETS:
        if low <= value < high:
            return label, low, high
    return PRICE_BUCKETS[-1]


def _reliability(samples: int) -> tuple[str, int]:
    if samples >= 500:
        return "высокая", 5
    if samples >= 200:
        return "хорошая", 4
    if samples >= 100:
        return "средняя", 3
    if samples >= 40:
        return "низкая", 2
    return "очень низкая", 1


def get_price_intelligence_report(
    checkpoint_minutes: Optional[int] = None,
    max_rows: Optional[int] = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    checkpoint = int(checkpoint_minutes or getattr(config, "PRICE_INTELLIGENCE_CHECKPOINT_MINUTES", 1440))
    limit = int(max_rows or getattr(config, "PRICE_INTELLIGENCE_MAX_ROWS", 5000))
    ttl = int(getattr(config, "PRICE_INTELLIGENCE_CACHE_SECONDS", 300))
    now = time.monotonic()
    if use_cache and _CACHE.get("report") is not None and now < float(_CACHE.get("expires") or 0):
        return dict(_CACHE["report"])

    try:
        with closing(get_connection()) as connection:
            rows = connection.execute(
                """
                SELECT s.entry_price, o.status, o.directional_return_percent
                FROM ai_signals s
                JOIN signal_outcomes o ON o.signal_id = s.signal_id
                WHERE o.checkpoint_minutes = ? AND o.status IS NOT NULL
                ORDER BY o.measured_at DESC
                LIMIT ?
                """,
                (checkpoint, max(1, limit)),
            ).fetchall()
    except sqlite3.OperationalError:
        rows = []

    prepared = [
        {
            "entry_price": max(0.0, _number(row[0])),
            "status": str(row[1] or "").upper(),
            "return": normalized_training_return(row[2]),
        }
        for row in rows
    ]
    total = len(prepared)
    global_strong = sum(item["status"] == "SUCCESS" for item in prepared)
    global_cont = sum(item["status"] in {"SUCCESS", "PARTIAL"} for item in prepared)
    global_strong_rate = global_strong / total * 100.0 if total else 0.0
    global_cont_rate = global_cont / total * 100.0 if total else 0.0

    min_samples = int(getattr(config, "PRICE_INTELLIGENCE_MIN_SAMPLES", 40))
    max_adjust = float(getattr(config, "PRICE_INTELLIGENCE_MAX_ADJUSTMENT", 8.0))
    shrinkage = float(getattr(config, "PRICE_INTELLIGENCE_SHRINKAGE_SAMPLES", 150.0))

    buckets: list[dict[str, Any]] = []
    for label, low, high in PRICE_BUCKETS:
        selected = [item for item in prepared if low <= item["entry_price"] < high]
        samples = len(selected)
        strong = sum(item["status"] == "SUCCESS" for item in selected)
        continuation = sum(item["status"] in {"SUCCESS", "PARTIAL"} for item in selected)
        strong_rate = strong / samples * 100.0 if samples else 0.0
        continuation_rate = continuation / samples * 100.0 if samples else 0.0
        avg_return = sum(item["return"] for item in selected) / samples if samples else 0.0

        # Strong continuation is primary; any continuation and normalized return
        # provide smaller confirmation. Shrink toward zero for small samples.
        raw_edge = (
            (strong_rate - global_strong_rate) * 0.42
            + (continuation_rate - global_cont_rate) * 0.18
            + avg_return * 0.12
        )
        reliability_weight = samples / (samples + shrinkage) if samples else 0.0
        adjustment = max(-max_adjust, min(max_adjust, raw_edge * reliability_weight))
        if samples < min_samples:
            adjustment = 0.0
        reliability, stars = _reliability(samples)
        buckets.append({
            "label": label,
            "low": low,
            "high": high,
            "samples": samples,
            "strong_rate": strong_rate,
            "continuation_rate": continuation_rate,
            "normalized_average_return": avg_return,
            "adjustment": adjustment,
            "reliability": reliability,
            "reliability_stars": stars,
        })

    report = {
        "checkpoint_minutes": checkpoint,
        "evaluated": total,
        "global_strong_rate": global_strong_rate,
        "global_continuation_rate": global_cont_rate,
        "buckets": buckets,
        "shadow_mode": True,
    }
    _CACHE["report"] = report
    _CACHE["expires"] = now + max(10, ttl)
    return dict(report)


def calculate_price_intelligence(alert: dict[str, Any]) -> dict[str, Any]:
    price = _entry_price(alert)
    label, low, high = price_bucket(price)
    report = get_price_intelligence_report()
    row = next((item for item in report.get("buckets", []) if item.get("label") == label), None)
    if row is None:
        row = {"samples": 0, "adjustment": 0.0, "strong_rate": 0.0,
               "continuation_rate": 0.0, "normalized_average_return": 0.0,
               "reliability": "нет данных", "reliability_stars": 0}
    return {
        "price_bucket": label,
        "price_intelligence_adjustment": round(float(row.get("adjustment") or 0.0), 2),
        "price_intelligence_samples": int(row.get("samples") or 0),
        "price_intelligence_strong_rate": round(float(row.get("strong_rate") or 0.0), 2),
        "price_intelligence_continuation_rate": round(float(row.get("continuation_rate") or 0.0), 2),
        "price_intelligence_normalized_return": round(float(row.get("normalized_average_return") or 0.0), 2),
        "price_intelligence_reliability": str(row.get("reliability") or "нет данных"),
        "price_intelligence_shadow_mode": True,
    }


def enrich_with_price_intelligence(alert: dict[str, Any]) -> dict[str, Any]:
    try:
        return {**alert, **calculate_price_intelligence(alert)}
    except Exception:
        label, _, _ = price_bucket(_entry_price(alert))
        return {
            **alert,
            "price_bucket": label,
            "price_intelligence_adjustment": 0.0,
            "price_intelligence_samples": 0,
            "price_intelligence_shadow_mode": True,
        }


def format_price_intelligence_report(report: dict[str, Any]) -> str:
    lines = [
        "💰 Price Intelligence · Shadow Mode",
        "",
        f"Проверено сигналов: {int(report.get('evaluated') or 0)}",
        f"База Strong: {float(report.get('global_strong_rate') or 0):.1f}%",
        "⚠️ Ценовой фактор пока не блокирует алерты.",
        "",
        "📊 Эффективность по стартовой цене",
    ]
    for item in report.get("buckets") or []:
        stars = "★" * int(item.get("reliability_stars") or 0) + "☆" * (5 - int(item.get("reliability_stars") or 0))
        adjustment = float(item.get("adjustment") or 0.0)
        lines.extend([
            f"• {item['label']} (n={int(item.get('samples') or 0)}) {stars}",
            f"  Strong {float(item.get('strong_rate') or 0):.1f}% · Любое {float(item.get('continuation_rate') or 0):.1f}%",
            f"  Норм. {float(item.get('normalized_average_return') or 0):+.1f}% · Confidence {adjustment:+.1f}",
        ])
    lines.extend([
        "",
        "Поправка рассчитывается динамически, с уменьшением влияния маленьких выборок.",
    ])
    return "\n".join(lines)
