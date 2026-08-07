"""Score Recalibration v1.0 — Shadow Mode.

Builds an empirical, shrinkage-adjusted alternative to the raw Score and audits
AI OPPORTUNITY separately. Diagnostic only: no live alert/filter/weight changes.
"""
from __future__ import annotations

import math
from collections import defaultdict
from contextlib import closing
from typing import Any

import config
from database import get_connection
from result_normalization import normalized_training_return

SCORE_BUCKETS = (("<40", None, 40), ("40–59", 40, 60), ("60–74", 60, 75), ("75–84", 75, 85), ("85+", 85, None))


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def score_bucket(value: Any) -> str:
    v = _num(value)
    for label, lo, hi in SCORE_BUCKETS:
        if lo is not None and v < lo:
            continue
        if hi is not None and v >= hi:
            continue
        return label
    return "<40"


def _load(checkpoint: int, limit: int) -> list[dict[str, Any]]:
    with closing(get_connection()) as con:
        rows = con.execute(
            """
            SELECT s.base_score, s.alert_type, s.category,
                   o.status, o.directional_return_percent
            FROM ai_signals s
            JOIN signal_outcomes o ON o.signal_id=s.signal_id
            WHERE o.checkpoint_minutes=? AND o.status IS NOT NULL
            ORDER BY o.measured_at DESC LIMIT ?
            """,
            (checkpoint, limit),
        ).fetchall()
    out = []
    for r in rows:
        typ = str(r[1] or "").upper()
        out.append({
            "score": _num(r[0]),
            "bucket": score_bucket(r[0]),
            "type": typ,
            "opportunity": "OPPORTUNITY" in typ,
            "category": str(r[2] or "OTHER").upper(),
            "status": str(r[3] or "").upper(),
            "strong": str(r[3] or "").upper() == "SUCCESS",
            "continued": str(r[3] or "").upper() in {"SUCCESS", "PARTIAL"},
            "ret": normalized_training_return(r[4]),
        })
    return out


def _stats(rows: list[dict[str, Any]]) -> dict[str, float]:
    n = len(rows)
    if not n:
        return {"n": 0, "strong": 0.0, "continued": 0.0, "ret": 0.0}
    return {
        "n": n,
        "strong": 100.0 * sum(x["strong"] for x in rows) / n,
        "continued": 100.0 * sum(x["continued"] for x in rows) / n,
        "ret": sum(x["ret"] for x in rows) / n,
    }


def _reliability(n: int, shrink: float) -> float:
    return n / (n + max(1.0, shrink))


def build_recalibration_map(rows: list[dict[str, Any]] | None = None) -> dict[str, dict[str, float]]:
    if rows is None:
        cp = int(getattr(config, "SCORE_RECALIBRATION_CHECKPOINT_MINUTES", 1440))
        limit = int(getattr(config, "SCORE_RECALIBRATION_MAX_ROWS", 5000))
        rows = _load(cp, limit)
    base = _stats(rows)
    shrink = float(getattr(config, "SCORE_RECALIBRATION_SHRINKAGE_SAMPLES", 150))
    max_adj = float(getattr(config, "SCORE_RECALIBRATION_MAX_ADJUSTMENT", 12.0))
    result: dict[str, dict[str, float]] = {}
    for label, _, __ in SCORE_BUCKETS:
        st = _stats([x for x in rows if x["bucket"] == label])
        rel = _reliability(int(st["n"]), shrink)
        # Strong continuation is primary; normalized return is a guardrail.
        raw_edge = 0.45 * (st["strong"] - base["strong"]) + 0.15 * (st["ret"] - base["ret"])
        adj = max(-max_adj, min(max_adj, raw_edge * rel))
        result[label] = {**st, "reliability": rel, "adjustment": adj}
    return result


def calculate_score_recalibration(base_score: Any, alert_type: str | None = None) -> dict[str, Any]:
    mapping = build_recalibration_map()
    label = score_bucket(base_score)
    item = mapping.get(label) or {"adjustment": 0.0, "n": 0, "reliability": 0.0}
    adjustment = float(item.get("adjustment") or 0.0)
    # OPPORTUNITY is audited separately; do not silently penalize it yet.
    return {
        "bucket": label,
        "adjustment": adjustment,
        "sample_size": int(item.get("n") or 0),
        "reliability": float(item.get("reliability") or 0.0),
        "shadow_mode": True,
    }


def get_score_recalibration_report(checkpoint_minutes: int | None = None, max_rows: int | None = None) -> dict[str, Any]:
    cp = int(checkpoint_minutes or getattr(config, "SCORE_RECALIBRATION_CHECKPOINT_MINUTES", 1440))
    limit = int(max_rows or getattr(config, "SCORE_RECALIBRATION_MAX_ROWS", 5000))
    rows = _load(cp, limit)
    base = _stats(rows)
    mapping = build_recalibration_map(rows)
    buckets = [{"label": label, **mapping[label]} for label, _, __ in SCORE_BUCKETS]

    opp = [x for x in rows if x["opportunity"]]
    non_opp = [x for x in rows if not x["opportunity"]]
    opp_stats = _stats(opp)
    non_opp_stats = _stats(non_opp)
    opp_buckets = []
    for label, _, __ in SCORE_BUCKETS:
        st = _stats([x for x in opp if x["bucket"] == label])
        if st["n"]:
            opp_buckets.append({"label": label, **st})

    categories = []
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in opp:
        grouped[x["category"]].append(x)
    for cat, vals in grouped.items():
        st = _stats(vals)
        if st["n"] >= 10:
            categories.append({"category": cat, **st})
    categories.sort(key=lambda x: (-x["n"], x["strong"]))

    return {
        "total": len(rows), "base": base, "buckets": buckets,
        "opportunity": opp_stats, "non_opportunity": non_opp_stats,
        "opportunity_buckets": opp_buckets, "opportunity_categories": categories[:6],
        "shadow_mode": True,
    }


def format_score_recalibration_report(r: dict[str, Any]) -> str:
    if not r.get("total"):
        return "🧭 Score Recalibration · Shadow Mode\n\nПока нет проверенных 24ч сигналов."
    b = r["base"]
    lines = [
        "🧭 Score Recalibration · Shadow Mode", "",
        f'Проверено сигналов: {r["total"]}',
        "⚠️ Новая калибровка не влияет на реальные алерты.", "",
        f'📊 База: Strong {b["strong"]:.1f}% · Любое {b["continued"]:.1f}% · Норм. {b["ret"]:+.1f}%', "",
        "🎚 Теневая перекалибровка Score",
    ]
    for x in r["buckets"]:
        lines.append(
            f'• {x["label"]}: Strong {x["strong"]:.1f}% · Норм. {x["ret"]:+.1f}% · '
            f'поправка {x["adjustment"]:+.1f} · n={int(x["n"])}'
        )
    o, no = r["opportunity"], r["non_opportunity"]
    lines += ["", "⭐ OPPORTUNITY Audit",
              f'• OPPORTUNITY: Strong {o["strong"]:.1f}% · Любое {o["continued"]:.1f}% · Норм. {o["ret"]:+.1f}% (n={int(o["n"])})',
              f'• Остальные: Strong {no["strong"]:.1f}% · Любое {no["continued"]:.1f}% · Норм. {no["ret"]:+.1f}% (n={int(no["n"])})']
    if r["opportunity_buckets"]:
        lines += ["", "🎯 OPPORTUNITY по Score"]
        for x in r["opportunity_buckets"]:
            lines.append(f'• {x["label"]}: Strong {x["strong"]:.1f}% · Норм. {x["ret"]:+.1f}% (n={int(x["n"])})')
    lines += ["", "ℹ️ Поправка строится на Strong + нормализованном результате с shrinkage по размеру выборки. OPPORTUNITY пока только аудируется и отдельно не штрафуется."]
    return "\n".join(lines)
