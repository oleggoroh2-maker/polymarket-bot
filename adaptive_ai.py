"""Adaptive AI weight proposals in shadow mode.

The module analyzes historical 24-hour outcomes and proposes calibration weights,
but never applies them to live signal scoring. Proposals are persisted for audit.
"""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import config
from database import get_connection
from feature_intelligence import get_feature_intelligence_report


CURRENT_WEIGHTS: dict[str, float] = {
    "score": 0.20,
    "ai_quality": 0.25,
    "ai_risk": 0.20,
    "ml": 0.15,
    "price_change": 0.10,
    "volume_change": 0.06,
    "liquidity_change": 0.04,
    "similarity": 0.00,
}

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


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalize(weights: dict[str, float]) -> dict[str, float]:
    cleaned = {key: max(0.0, float(value)) for key, value in weights.items()}
    total = sum(cleaned.values())
    if total <= 0:
        return dict(CURRENT_WEIGHTS)
    return {key: value / total for key, value in cleaned.items()}


def ensure_adaptive_schema() -> None:
    with closing(get_connection()) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS adaptive_weight_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                checkpoint_minutes INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                confidence TEXT NOT NULL,
                current_weights_json TEXT NOT NULL,
                proposed_weights_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                proposal_hash TEXT NOT NULL UNIQUE
            );

            CREATE INDEX IF NOT EXISTS idx_adaptive_proposals_created
            ON adaptive_weight_proposals (created_at DESC);
            """
        )
        connection.commit()


def _factor_evidence(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for factor in report.get("factors") or []:
        key = str(factor.get("key") or "")
        if key not in CURRENT_WEIGHTS:
            continue
        best = factor.get("best") or {}
        evidence[key] = {
            "importance": float(factor.get("effective_importance") or factor.get("importance") or 0.0),
            "best_range": str(best.get("label") or "—"),
            "continuation_rate": float(best.get("continuation_rate") or 0.0),
            "samples": int(best.get("samples") or 0),
        }
    return evidence


def _confidence(total: int, evidence_count: int, similarity_samples: int) -> str:
    min_samples = int(getattr(config, "ADAPTIVE_AI_MIN_SAMPLES", 500))
    if total < min_samples or evidence_count < 4:
        return "LOW"
    if total >= 1500 and evidence_count >= 6 and similarity_samples >= 100:
        return "HIGH"
    return "MEDIUM"


def _propose_weights(report: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    evidence = _factor_evidence(report)
    total = int(report.get("total") or 0)
    similarity_samples = int(report.get("similarity_samples") or 0)
    similarity_min = int(getattr(config, "ADAPTIVE_AI_SIMILARITY_MIN_SAMPLES", 100))

    usable: dict[str, float] = {}
    for key in CURRENT_WEIGHTS:
        item = evidence.get(key)
        if not item:
            continue
        if key == "similarity" and similarity_samples < similarity_min:
            continue
        # Importance is separation across buckets. Sample support dampens tiny buckets.
        support = min(1.0, max(0.0, item["samples"] / 250.0))
        usable[key] = max(0.1, item["importance"] * (0.55 + 0.45 * support))

    if len(usable) < 3:
        return dict(CURRENT_WEIGHTS), {
            "total": total,
            "similarity_samples": similarity_samples,
            "evidence": evidence,
            "reason": "Недостаточно надёжных факторов для предложения.",
        }

    # Preserve a small prior for every established factor; new Similarity gets a
    # prior only when enough evaluated samples exist.
    target_raw: dict[str, float] = {}
    for key, current in CURRENT_WEIGHTS.items():
        if key == "similarity" and similarity_samples < similarity_min:
            target_raw[key] = 0.0
            continue
        signal = usable.get(key, 0.0)
        target_raw[key] = 0.35 * current + 0.65 * signal / 100.0

    target = _normalize(target_raw)
    blend = float(getattr(config, "ADAPTIVE_AI_SHADOW_BLEND", 0.30))
    blend = _clip(blend, 0.05, 0.50)
    max_delta = float(getattr(config, "ADAPTIVE_AI_MAX_PROPOSAL_DELTA", 0.05))
    max_delta = _clip(max_delta, 0.01, 0.10)

    proposed: dict[str, float] = {}
    for key, current in CURRENT_WEIGHTS.items():
        raw = current + (target.get(key, 0.0) - current) * blend
        proposed[key] = _clip(raw, max(0.0, current - max_delta), current + max_delta)

    proposed = _normalize(proposed)
    return proposed, {
        "total": total,
        "similarity_samples": similarity_samples,
        "evidence": evidence,
        "reason": "Shadow Mode: веса рассчитаны, но не применены.",
    }


def generate_weight_proposal(
    checkpoint_minutes: int | None = None,
    max_rows: int | None = None,
    min_bucket_samples: int | None = None,
    save: bool = True,
) -> dict[str, Any]:
    ensure_adaptive_schema()
    checkpoint = int(
        checkpoint_minutes
        if checkpoint_minutes is not None
        else getattr(config, "ADAPTIVE_AI_CHECKPOINT_MINUTES", 1440)
    )
    report = get_feature_intelligence_report(
        checkpoint_minutes=checkpoint,
        max_rows=int(max_rows if max_rows is not None else getattr(config, "ADAPTIVE_AI_MAX_ROWS", 5000)),
        min_bucket_samples=int(
            min_bucket_samples
            if min_bucket_samples is not None
            else getattr(config, "ADAPTIVE_AI_MIN_BUCKET_SAMPLES", 20)
        ),
        save=True,
    )
    proposed, details = _propose_weights(report)
    confidence = _confidence(
        int(report.get("total") or 0),
        len(details.get("evidence") or {}),
        int(report.get("similarity_samples") or 0),
    )
    result = {
        "created_at": _now_iso(),
        "checkpoint_minutes": checkpoint,
        "sample_count": int(report.get("total") or 0),
        "confidence": confidence,
        "current_weights": dict(CURRENT_WEIGHTS),
        "proposed_weights": proposed,
        "details": details,
        "applied": False,
    }

    if save and result["sample_count"] > 0:
        canonical = json.dumps(
            {
                "date": result["created_at"][:10],
                "samples": result["sample_count"],
                "proposed": {k: round(v, 4) for k, v in proposed.items()},
            },
            sort_keys=True,
        )
        proposal_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        with closing(get_connection()) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO adaptive_weight_proposals (
                    created_at, checkpoint_minutes, sample_count, confidence,
                    current_weights_json, proposed_weights_json,
                    evidence_json, proposal_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["created_at"], checkpoint, result["sample_count"], confidence,
                    json.dumps(result["current_weights"], ensure_ascii=False),
                    json.dumps(proposed, ensure_ascii=False),
                    json.dumps(details, ensure_ascii=False),
                    proposal_hash,
                ),
            )
            connection.commit()
    return result


def get_proposal_history(limit: int = 5) -> list[dict[str, Any]]:
    ensure_adaptive_schema()
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT created_at, sample_count, confidence, proposed_weights_json
            FROM adaptive_weight_proposals
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    history: list[dict[str, Any]] = []
    for created_at, samples, confidence, raw_weights in rows:
        try:
            weights = json.loads(raw_weights or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            weights = {}
        history.append({
            "created_at": str(created_at),
            "sample_count": int(samples or 0),
            "confidence": str(confidence or "LOW"),
            "weights": weights if isinstance(weights, dict) else {},
        })
    return history


def format_weight_proposal(result: dict[str, Any]) -> str:
    samples = int(result.get("sample_count") or 0)
    if samples == 0:
        return "🧠 Adaptive AI · Shadow Mode\n\nПока нет проверенных сигналов."

    confidence_map = {"LOW": "низкая", "MEDIUM": "средняя", "HIGH": "высокая"}
    confidence = str(result.get("confidence") or "LOW").upper()
    current = result.get("current_weights") or {}
    proposed = result.get("proposed_weights") or {}
    evidence = (result.get("details") or {}).get("evidence") or {}

    lines = [
        "🧠 Adaptive AI · Shadow Mode",
        "",
        f"Проверено сигналов: {samples}",
        f"Уверенность рекомендации: {confidence_map.get(confidence, confidence)}",
        "⚠️ Новые веса НЕ применяются к алертам.",
        "",
        "⚖️ Текущие → предлагаемые веса",
    ]
    for key in CURRENT_WEIGHTS:
        old = float(current.get(key, 0.0)) * 100.0
        new = float(proposed.get(key, 0.0)) * 100.0
        delta = new - old
        marker = "➖" if abs(delta) < 0.05 else ("⬆️" if delta > 0 else "⬇️")
        lines.append(f"{marker} {LABELS[key]}: {old:.1f}% → {new:.1f}% ({delta:+.1f} п.п.)")

    ranked = sorted(
        evidence.items(),
        key=lambda item: float((item[1] or {}).get("importance") or 0.0),
        reverse=True,
    )
    if ranked:
        lines.extend(["", "📊 Главные основания"])
        for key, item in ranked[:4]:
            lines.append(
                f"• {LABELS.get(key, key)}: {float(item.get('importance') or 0):.1f}/100 · "
                f"лучший диапазон {item.get('best_range', '—')} · n={int(item.get('samples') or 0)}"
            )

    similarity_samples = int((result.get("details") or {}).get("similarity_samples") or 0)
    if similarity_samples < int(getattr(config, "ADAPTIVE_AI_SIMILARITY_MIN_SAMPLES", 100)):
        lines.extend([
            "",
            f"ℹ️ Similarity проверен только у {similarity_samples} сигналов и пока не получает вес.",
        ])

    lines.extend([
        "",
        "Рекомендации сохраняются в журнал для сравнения стабильности по дням.",
    ])
    return "\n".join(lines)
