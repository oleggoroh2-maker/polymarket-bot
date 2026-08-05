"""AI Simulator: compare current and Adaptive AI shadow weights without live impact.

The simulator stores two composite scores for every newly recorded alert. Once the
24-hour AI Memory outcome exists, it compares equal-sized top-ranked groups from
the current and shadow models. It never changes alert delivery or live scoring.
"""

from __future__ import annotations

import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import config
from adaptive_ai import CURRENT_WEIGHTS, generate_weight_proposal, get_proposal_history
from database import get_connection
from result_normalization import normalized_training_return
from price_intelligence import calculate_price_intelligence


LABELS = {
    "score": "Score",
    "ai_quality": "AI Quality",
    "ai_risk": "AI Risk (обратно)",
    "ml": "ML",
    "price_change": "Движение цены",
    "volume_change": "Изм. объёма",
    "liquidity_change": "Изм. ликвидности",
    "similarity": "Similarity",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _probability_percent(value: Any) -> float:
    result = _number(value)
    if 0.0 <= result <= 1.0:
        result *= 100.0
    return _clip(result)


def ensure_simulator_schema() -> None:
    with closing(get_connection()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_simulator_signals (
                signal_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                current_score REAL NOT NULL,
                shadow_score REAL NOT NULL,
                price_score REAL,
                current_weights_json TEXT NOT NULL,
                shadow_weights_json TEXT NOT NULL,
                factors_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_ai_simulator_created
            ON ai_simulator_signals (created_at DESC);
            """
        )
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ai_simulator_signals)").fetchall()}
        if "price_score" not in columns:
            connection.execute("ALTER TABLE ai_simulator_signals ADD COLUMN price_score REAL")
        connection.commit()


def _normalize_weights(weights: dict[str, Any]) -> dict[str, float]:
    values = {
        key: max(0.0, _number(weights.get(key), CURRENT_WEIGHTS.get(key, 0.0)))
        for key in CURRENT_WEIGHTS
    }
    total = sum(values.values())
    if total <= 0:
        return dict(CURRENT_WEIGHTS)
    return {key: value / total for key, value in values.items()}


def _latest_shadow_weights() -> dict[str, float]:
    history = get_proposal_history(1)
    if history:
        raw = history[0].get("weights") or {}
        if isinstance(raw, dict) and raw:
            return _normalize_weights(raw)

    # First run: create one recommendation, still without applying it.
    try:
        proposal = generate_weight_proposal(save=True)
        raw = proposal.get("proposed_weights") or {}
        if isinstance(raw, dict) and raw:
            return _normalize_weights(raw)
    except Exception:
        pass
    return dict(CURRENT_WEIGHTS)


def extract_factors(alert: dict[str, Any], assessment: dict[str, Any]) -> dict[str, float]:
    """Convert alert metrics into comparable 0..100 factor strengths."""
    price_change = alert.get("change_percent")
    if price_change is None:
        price_change = alert.get("change")

    volume_change = alert.get("volume_change_percent")
    liquidity_change = alert.get("liquidity_change_percent")
    similarity = alert.get("similarity_average")

    return {
        "score": _clip(_number(alert.get("score"))),
        "ai_quality": _clip(_number(assessment.get("ai_quality"))),
        "ai_risk": _clip(100.0 - _number(assessment.get("ai_risk"))),
        "ml": _probability_percent(assessment.get("ml_probability")),
        "price_change": _clip(abs(_number(price_change))),
        # Falling volume/liquidity does not count as positive confirmation.
        "volume_change": _clip(max(0.0, _number(volume_change))),
        "liquidity_change": _clip(max(0.0, _number(liquidity_change))),
        "similarity": _clip(_number(similarity)),
    }


def calculate_composite_score(
    factors: dict[str, float],
    weights: dict[str, float],
) -> float:
    normalized = _normalize_weights(weights)
    return sum(_clip(factors.get(key, 0.0)) * normalized[key] for key in normalized)


def record_simulator_signal(
    signal_id: str,
    alert: dict[str, Any],
    assessment: dict[str, Any],
) -> None:
    """Persist current/shadow scores for a newly stored alert."""
    ensure_simulator_schema()
    factors = extract_factors(alert, assessment)
    current_weights = dict(CURRENT_WEIGHTS)
    shadow_weights = _latest_shadow_weights()
    current_score = calculate_composite_score(factors, current_weights)
    shadow_score = calculate_composite_score(factors, shadow_weights)
    price_data = calculate_price_intelligence(alert)
    price_adjustment = float(price_data.get("price_intelligence_adjustment") or 0.0)
    price_score = _clip(shadow_score + price_adjustment)

    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO ai_simulator_signals (
                signal_id, created_at, current_score, shadow_score, price_score,
                current_weights_json, shadow_weights_json, factors_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(signal_id), _now_iso(), current_score, shadow_score, price_score,
                json.dumps(current_weights, ensure_ascii=False),
                json.dumps(shadow_weights, ensure_ascii=False),
                json.dumps(factors, ensure_ascii=False),
            ),
        )
        connection.commit()


def _model_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    strong = sum(1 for row in rows if row["status"] == "SUCCESS")
    continued = sum(1 for row in rows if row["status"] in {"SUCCESS", "PARTIAL"})
    neutral = sum(1 for row in rows if row["status"] == "NEUTRAL")
    failed = sum(1 for row in rows if row["status"] == "FAIL")
    average = sum(row["return"] for row in rows) / total if total else 0.0
    return {
        "total": total,
        "strong": strong,
        "continued": continued,
        "neutral": neutral,
        "failed": failed,
        "strong_rate": strong / total * 100.0 if total else 0.0,
        "continuation_rate": continued / total * 100.0 if total else 0.0,
        "average_return": average,
    }


def get_simulator_report(
    checkpoint_minutes: int | None = None,
    max_rows: int | None = None,
    top_fraction: float | None = None,
) -> dict[str, Any]:
    """Compare equal-sized top-ranked groups for both weight sets."""
    ensure_simulator_schema()
    checkpoint = int(
        checkpoint_minutes
        if checkpoint_minutes is not None
        else getattr(config, "AI_SIMULATOR_CHECKPOINT_MINUTES", 1440)
    )
    row_limit = int(
        max_rows if max_rows is not None else getattr(config, "AI_SIMULATOR_MAX_ROWS", 3000)
    )
    fraction = float(
        top_fraction
        if top_fraction is not None
        else getattr(config, "AI_SIMULATOR_TOP_FRACTION", 0.35)
    )
    fraction = max(0.10, min(1.0, fraction))

    try:
        with closing(get_connection()) as connection:
            raw_rows = connection.execute(
                """
                SELECT sim.signal_id, sim.current_score, sim.shadow_score, sim.price_score,
                       o.status, o.directional_return_percent, s.title,
                       sim.created_at
                FROM ai_simulator_signals sim
                JOIN signal_outcomes o ON o.signal_id = sim.signal_id
                JOIN ai_signals s ON s.signal_id = sim.signal_id
                WHERE o.checkpoint_minutes = ? AND o.status IS NOT NULL
                ORDER BY o.measured_at DESC
                LIMIT ?
                """,
                (checkpoint, max(1, row_limit)),
            ).fetchall()
    except sqlite3.OperationalError:
        raw_rows = []

    rows = [
        {
            "signal_id": str(row[0]),
            "current_score": float(row[1]),
            "shadow_score": float(row[2]),
            "price_score": float(row[3] if row[3] is not None else row[2]),
            "status": str(row[4]).upper(),
            "return": normalized_training_return(row[5]),
            "raw_return": float(row[5] or 0.0),
            "title": str(row[6] or ""),
            "created_at": str(row[7] or ""),
        }
        for row in raw_rows
    ]

    total = len(rows)
    minimum = int(getattr(config, "AI_SIMULATOR_MIN_EVALUATED", 30))
    if not rows:
        return {
            "checkpoint_minutes": checkpoint,
            "evaluated": 0,
            "minimum": minimum,
            "ready": False,
            "top_count": 0,
        }

    top_count = max(1, int(round(total * fraction)))
    current_top = sorted(rows, key=lambda item: item["current_score"], reverse=True)[:top_count]
    shadow_top = sorted(rows, key=lambda item: item["shadow_score"], reverse=True)[:top_count]
    price_top = sorted(rows, key=lambda item: item["price_score"], reverse=True)[:top_count]

    current_ids = {row["signal_id"] for row in current_top}
    shadow_ids = {row["signal_id"] for row in shadow_top}
    price_ids = {row["signal_id"] for row in price_top}
    overlap = len(current_ids & shadow_ids)
    promoted = [row for row in shadow_top if row["signal_id"] not in current_ids]
    demoted = [row for row in current_top if row["signal_id"] not in shadow_ids]

    current_stats = _model_stats(current_top)
    shadow_stats = _model_stats(shadow_top)
    price_stats = _model_stats(price_top)
    return {
        "checkpoint_minutes": checkpoint,
        "evaluated": total,
        "minimum": minimum,
        "ready": total >= minimum,
        "top_fraction": fraction,
        "top_count": top_count,
        "current": current_stats,
        "shadow": shadow_stats,
        "price_intelligence": price_stats,
        "strong_delta": shadow_stats["strong_rate"] - current_stats["strong_rate"],
        "continuation_delta": shadow_stats["continuation_rate"] - current_stats["continuation_rate"],
        "return_delta": shadow_stats["average_return"] - current_stats["average_return"],
        "price_strong_delta": price_stats["strong_rate"] - current_stats["strong_rate"],
        "price_continuation_delta": price_stats["continuation_rate"] - current_stats["continuation_rate"],
        "price_return_delta": price_stats["average_return"] - current_stats["average_return"],
        "price_overlap": len(current_ids & price_ids),
        "overlap": overlap,
        "promoted": promoted[:5],
        "demoted": demoted[:5],
    }


def format_simulator_report(report: dict[str, Any]) -> str:
    evaluated = int(report.get("evaluated") or 0)
    minimum = int(report.get("minimum") or 0)
    if evaluated == 0:
        return (
            "🧪 AI Simulator · Shadow Mode\n\n"
            "Пока нет сигналов, которые были записаны симулятором и прошли 24-часовую проверку.\n"
            "Первые результаты появятся примерно через 24 часа после обновления."
        )

    top_count = int(report.get("top_count") or 0)
    fraction = float(report.get("top_fraction") or 0.0) * 100.0
    current = report.get("current") or {}
    shadow = report.get("shadow") or {}
    price_model = report.get("price_intelligence") or {}

    lines = [
        "🧪 AI Simulator · Shadow Mode",
        "",
        f"Проверено новых сигналов: {evaluated}",
        f"Сравнение: лучшие {top_count} ({fraction:.0f}%) по каждой модели",
        "⚠️ Теневые веса не влияют на реальные алерты.",
        "",
        "⚙️ Текущие веса",
        f"✅ Сильное продолжение: {float(current.get('strong_rate') or 0):.1f}%",
        f"🟡 Любое продолжение: {float(current.get('continuation_rate') or 0):.1f}%",
        f"📈 Нормализованный результат: {float(current.get('average_return') or 0):+.1f}%",
        "",
        "🧠 Теневые веса",
        f"✅ Сильное продолжение: {float(shadow.get('strong_rate') or 0):.1f}%",
        f"🟡 Любое продолжение: {float(shadow.get('continuation_rate') or 0):.1f}%",
        f"📈 Нормализованный результат: {float(shadow.get('average_return') or 0):+.1f}%",
        "",
        "💰 Теневая модель + Price Intelligence",
        f"✅ Сильное продолжение: {float(price_model.get('strong_rate') or 0):.1f}%",
        f"🟡 Любое продолжение: {float(price_model.get('continuation_rate') or 0):.1f}%",
        f"📈 Нормализованный результат: {float(price_model.get('average_return') or 0):+.1f}%",
        "",
        "📊 Разница теневой модели",
        f"• Сильное: {float(report.get('strong_delta') or 0):+.1f} п.п.",
        f"• Любое: {float(report.get('continuation_delta') or 0):+.1f} п.п.",
        f"• Средний результат: {float(report.get('return_delta') or 0):+.1f} п.п.",
        f"• Совпало в топе: {int(report.get('overlap') or 0)}/{top_count}",
        "",
        "💰 Разница Price Intelligence",
        f"• Сильное: {float(report.get('price_strong_delta') or 0):+.1f} п.п.",
        f"• Любое: {float(report.get('price_continuation_delta') or 0):+.1f} п.п.",
        f"• Средний результат: {float(report.get('price_return_delta') or 0):+.1f} п.п.",
        f"• Совпало в топе: {int(report.get('price_overlap') or 0)}/{top_count}",
    ]

    if not report.get("ready"):
        lines.extend([
            "",
            f"ℹ️ Выборка пока мала: нужно минимум {minimum} проверенных сигналов.",
        ])
    else:
        strong_delta = float(report.get("strong_delta") or 0.0)
        continuation_delta = float(report.get("continuation_delta") or 0.0)
        return_delta = float(report.get("return_delta") or 0.0)
        if strong_delta > 0 and continuation_delta >= 0 and return_delta > 0:
            verdict = "🟢 Теневая модель пока выглядит лучше."
        elif strong_delta < 0 and continuation_delta <= 0 and return_delta < 0:
            verdict = "🔴 Теневая модель пока выглядит хуже."
        else:
            verdict = "🟡 Результат смешанный — нужно больше данных."
        lines.extend(["", verdict])

    lines.extend([
        "",
        "Сравнение выполняется на одинаковом количестве сигналов, поэтому модели оцениваются честно.",
    ])
    return "\n".join(lines)
