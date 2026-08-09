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
from score_recalibration import calculate_score_recalibration
from combination_intelligence import calculate_combination_adjustment


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
                recal_score REAL,
                no_opportunity_score REAL,
                combination_score REAL,
                combination_no_opportunity_score REAL,
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
        for name in ("recal_score", "no_opportunity_score", "combination_score", "combination_no_opportunity_score"):
            if name not in columns:
                connection.execute(f"ALTER TABLE ai_simulator_signals ADD COLUMN {name} REAL")
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
    recal = calculate_score_recalibration(alert.get("score"), alert.get("alert_type"))
    recal_score = _clip(price_score + float(recal.get("adjustment") or 0.0))
    is_opportunity = "OPPORTUNITY" in str(alert.get("alert_type") or "").upper()
    opportunity_penalty = float(getattr(config, "AI_SIMULATOR_OPPORTUNITY_PENALTY", 100.0))
    no_opportunity_score = _clip(recal_score - (opportunity_penalty if is_opportunity else 0.0))
    combo_input = {
        **alert,
        "ai_quality": assessment.get("ai_quality"),
        "ai_risk": assessment.get("ai_risk"),
        "ml_probability": assessment.get("ml_probability"),
        "entry_price": alert.get("current_price", alert.get("price")),
    }
    combo = calculate_combination_adjustment(combo_input)
    combo_score = _clip(recal_score + float(combo.get("adjustment") or 0.0))
    combination_no_opportunity_score = _clip(combo_score - (opportunity_penalty if is_opportunity else 0.0))

    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO ai_simulator_signals (
                signal_id, created_at, current_score, shadow_score, price_score,
                recal_score, no_opportunity_score, combination_score, combination_no_opportunity_score,
                current_weights_json, shadow_weights_json, factors_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(signal_id), _now_iso(), current_score, shadow_score, price_score,
                recal_score, no_opportunity_score, combo_score, combination_no_opportunity_score,
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
                       sim.recal_score, sim.no_opportunity_score, sim.combination_score, sim.combination_no_opportunity_score,
                       o.status, o.directional_return_percent, s.title,
                       sim.created_at, s.base_score, s.alert_type
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
            "recal_score": None if row[4] is None else float(row[4]),
            "no_opportunity_score": None if row[5] is None else float(row[5]),
            "combination_score": None if row[6] is None else float(row[6]),
            "combination_no_opportunity_score": None if row[7] is None else float(row[7]),
            "status": str(row[8]).upper(),
            "return": normalized_training_return(row[9]),
            "raw_return": float(row[9] or 0.0),
            "title": str(row[10] or ""),
            "created_at": str(row[11] or ""),
            "base_score": float(row[12] or 0.0),
            "alert_type": str(row[13] or ""),
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
    # Legacy rows predate Candidate Strategies v2. They remain in the old model
    # comparisons, but candidate strategies use only scores frozen at signal time.
    price_top = sorted(rows, key=lambda item: item["price_score"], reverse=True)[:top_count]
    for item in rows:
        recal = calculate_score_recalibration(item["base_score"], item["alert_type"])
        item["legacy_recalibrated_score"] = _clip(item["price_score"] + float(recal.get("adjustment") or 0.0))
    recal_top = sorted(rows, key=lambda item: item["legacy_recalibrated_score"], reverse=True)[:top_count]
    candidate_rows = [row for row in rows if row.get("recal_score") is not None]
    candidate_top_count = max(1, int(round(len(candidate_rows) * fraction))) if candidate_rows else 0
    candidate_recal_top = sorted(candidate_rows, key=lambda item: item["recal_score"], reverse=True)[:candidate_top_count]
    no_opp_top = sorted(candidate_rows, key=lambda item: item["no_opportunity_score"], reverse=True)[:candidate_top_count]
    combo_top = sorted(candidate_rows, key=lambda item: item["combination_score"], reverse=True)[:candidate_top_count]
    combo_no_opp_top = sorted(candidate_rows, key=lambda item: item["combination_no_opportunity_score"], reverse=True)[:candidate_top_count]

    current_ids = {row["signal_id"] for row in current_top}
    shadow_ids = {row["signal_id"] for row in shadow_top}
    price_ids = {row["signal_id"] for row in price_top}
    recal_ids = {row["signal_id"] for row in recal_top}
    candidate_recal_ids = {row["signal_id"] for row in candidate_recal_top}
    no_opp_ids = {row["signal_id"] for row in no_opp_top}
    combo_ids = {row["signal_id"] for row in combo_top}
    combo_no_opp_ids = {row["signal_id"] for row in combo_no_opp_top}
    overlap = len(current_ids & shadow_ids)
    promoted = [row for row in shadow_top if row["signal_id"] not in current_ids]
    demoted = [row for row in current_top if row["signal_id"] not in shadow_ids]

    current_stats = _model_stats(current_top)
    shadow_stats = _model_stats(shadow_top)
    price_stats = _model_stats(price_top)
    recal_stats = _model_stats(recal_top)
    candidate_recal_stats = _model_stats(candidate_recal_top)
    no_opp_stats = _model_stats(no_opp_top)
    combo_stats = _model_stats(combo_top)
    combo_no_opp_stats = _model_stats(combo_no_opp_top)
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
        "score_recalibration": recal_stats,
        "candidate_evaluated": len(candidate_rows),
        "candidate_recalibration": candidate_recal_stats,
        "candidate_top_count": candidate_top_count,
        "no_opportunity": no_opp_stats,
        "combination": combo_stats,
        "combination_no_opportunity": combo_no_opp_stats,
        "strong_delta": shadow_stats["strong_rate"] - current_stats["strong_rate"],
        "continuation_delta": shadow_stats["continuation_rate"] - current_stats["continuation_rate"],
        "return_delta": shadow_stats["average_return"] - current_stats["average_return"],
        "price_strong_delta": price_stats["strong_rate"] - current_stats["strong_rate"],
        "price_continuation_delta": price_stats["continuation_rate"] - current_stats["continuation_rate"],
        "price_return_delta": price_stats["average_return"] - current_stats["average_return"],
        "price_overlap": len(current_ids & price_ids),
        "recal_strong_delta": recal_stats["strong_rate"] - current_stats["strong_rate"],
        "recal_continuation_delta": recal_stats["continuation_rate"] - current_stats["continuation_rate"],
        "recal_return_delta": recal_stats["average_return"] - current_stats["average_return"],
        "recal_overlap": len(current_ids & recal_ids),
        "candidate_recal_overlap": len(candidate_recal_ids & no_opp_ids),
        "combination_overlap": len(candidate_recal_ids & combo_ids),
        "combination_no_opp_overlap": len(candidate_recal_ids & combo_no_opp_ids),
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
    recal_model = report.get("score_recalibration") or {}

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
        "🧭 + Score Recalibration",
        f"✅ Сильное продолжение: {float(recal_model.get('strong_rate') or 0):.1f}%",
        f"🟡 Любое продолжение: {float(recal_model.get('continuation_rate') or 0):.1f}%",
        f"📈 Нормализованный результат: {float(recal_model.get('average_return') or 0):+.1f}%",
        "",
    ]
    candidate_n = int(report.get("candidate_evaluated") or 0)
    candidate_top = int(report.get("candidate_top_count") or 0)
    if candidate_n:
        candidate_recal = report.get("candidate_recalibration") or {}
        no_opp = report.get("no_opportunity") or {}
        combo = report.get("combination") or {}
        combo_no = report.get("combination_no_opportunity") or {}
        lines.extend([
            "🧪 Candidate Strategies v2 · только сигналы после обновления",
            f"Проверено: {candidate_n} · TOP: {candidate_top}",
            "",
            "🧭 Frozen Recalibration",
            f"✅ Strong: {float(candidate_recal.get('strong_rate') or 0):.1f}% · 🟡 Любое: {float(candidate_recal.get('continuation_rate') or 0):.1f}% · 📈 {float(candidate_recal.get('average_return') or 0):+.1f}%",
            "🚫 Recalibration + No OPPORTUNITY",
            f"✅ Strong: {float(no_opp.get('strong_rate') or 0):.1f}% · 🟡 Любое: {float(no_opp.get('continuation_rate') or 0):.1f}% · 📈 {float(no_opp.get('average_return') or 0):+.1f}%",
            "🧩 Recalibration + Combination bonus",
            f"✅ Strong: {float(combo.get('strong_rate') or 0):.1f}% · 🟡 Любое: {float(combo.get('continuation_rate') or 0):.1f}% · 📈 {float(combo.get('average_return') or 0):+.1f}%",
            "🏆 Recalibration + Combinations + No OPPORTUNITY",
            f"✅ Strong: {float(combo_no.get('strong_rate') or 0):.1f}% · 🟡 Любое: {float(combo_no.get('continuation_rate') or 0):.1f}% · 📈 {float(combo_no.get('average_return') or 0):+.1f}%",
            "",
        ])
    else:
        lines.extend([
            "🧪 Candidate Strategies v2",
            "Пока нет 24ч результатов сигналов, записанных после обновления.",
            "",
        ])
    lines.extend([
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
        "",
        "🧭 Разница Score Recalibration",
        f"• Сильное: {float(report.get('recal_strong_delta') or 0):+.1f} п.п.",
        f"• Любое: {float(report.get('recal_continuation_delta') or 0):+.1f} п.п.",
        f"• Средний результат: {float(report.get('recal_return_delta') or 0):+.1f} п.п.",
        f"• Совпало в топе: {int(report.get('recal_overlap') or 0)}/{top_count}",
    ])

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
