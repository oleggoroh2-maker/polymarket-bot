"""Combination Intelligence Engine v1.0 — Shadow Mode.

Finds historical combinations of signal characteristics that outperform or
underperform the global 24h baseline. It is diagnostic only: no alert is
blocked, promoted or re-scored by this module.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from itertools import combinations
from typing import Any

import config
from database import get_connection
from result_normalization import entry_price_bucket, normalized_training_return


LABELS = {
    "score": "Score",
    "ai_quality": "AI Quality",
    "ai_risk": "AI Risk",
    "ml": "ML",
    "liquidity": "Ликвидность",
    "price_bucket": "Цена",
    "liquidity_change": "Δ ликвидности",
    "volume_change": "Δ объёма",
    "similarity": "Similarity",
    "category": "Категория",
    "direction": "Направление",
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _probability(value: Any) -> float | None:
    if value is None:
        return None
    number = _number(value, float("nan"))
    if not math.isfinite(number):
        return None
    if 0.0 <= number <= 1.0:
        number *= 100.0
    return max(0.0, min(100.0, number))


def _metadata(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _bucket(value: float | None, cuts: list[tuple[str, float | None, float | None]]) -> str | None:
    if value is None:
        return None
    for label, low, high in cuts:
        if low is not None and value < low:
            continue
        if high is not None and value >= high:
            continue
        return label
    return None


BUCKETS = {
    "score": [("<40", None, 40), ("40–59", 40, 60), ("60–74", 60, 75), ("75–84", 75, 85), ("85+", 85, None)],
    "ai_quality": [("<40", None, 40), ("40–59", 40, 60), ("60–74", 60, 75), ("75–84", 75, 85), ("85+", 85, None)],
    "ai_risk": [("0–19", None, 20), ("20–39", 20, 40), ("40–59", 40, 60), ("60+", 60, None)],
    "ml": [("<10%", None, 10), ("10–24%", 10, 25), ("25–39%", 25, 40), ("40–59%", 40, 60), ("60%+", 60, None)],
    "liquidity": [("<$10k", None, 10_000), ("$10–50k", 10_000, 50_000), ("$50–250k", 50_000, 250_000), ("$250k–1M", 250_000, 1_000_000), ("$1M+", 1_000_000, None)],
    "liquidity_change": [("<-10%", None, -10), ("-10–0%", -10, 0), ("0–10%", 0, 10), ("10–30%", 10, 30), ("30%+", 30, None)],
    "volume_change": [("<-20%", None, -20), ("-20–0%", -20, 0), ("0–20%", 0, 20), ("20–80%", 20, 80), ("80%+", 80, None)],
    "similarity": [("<60%", None, 60), ("60–69%", 60, 70), ("70–79%", 70, 80), ("80–89%", 80, 90), ("90%+", 90, None)],
}


def _load_rows(checkpoint: int, limit: int) -> list[dict[str, Any]]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT s.base_score, s.ai_quality, s.ai_risk, s.ml_probability,
                   s.liquidity, s.entry_price, s.category, s.alert_type,
                   s.metadata_json, o.status, o.directional_return_percent
            FROM ai_signals s
            JOIN signal_outcomes o ON o.signal_id = s.signal_id
            WHERE o.checkpoint_minutes = ? AND o.status IS NOT NULL
            ORDER BY o.measured_at DESC
            LIMIT ?
            """,
            (int(checkpoint), int(limit)),
        ).fetchall()

    result: list[dict[str, Any]] = []
    for row in rows:
        meta = _metadata(row[8])
        status = str(row[9] or "").upper()
        ml = _probability(row[3])
        sim_raw = meta.get("similarity_average")
        sim = None if sim_raw is None else _number(sim_raw)
        if sim is not None and 0 <= sim <= 1:
            sim *= 100.0
        liq_change = None if meta.get("liquidity_change_percent") is None else _number(meta.get("liquidity_change_percent"))
        vol_change = None if meta.get("volume_change_percent") is None else _number(meta.get("volume_change_percent"))
        alert_type = str(row[7] or "").upper()
        category = str(row[6] or "OTHER").upper() or "OTHER"
        dims = {
            "score": _bucket(_number(row[0]), BUCKETS["score"]),
            "ai_quality": _bucket(_number(row[1]), BUCKETS["ai_quality"]),
            "ai_risk": _bucket(_number(row[2]), BUCKETS["ai_risk"]),
            "ml": _bucket(ml, BUCKETS["ml"]),
            "liquidity": _bucket(max(0.0, _number(row[4])), BUCKETS["liquidity"]),
            "price_bucket": entry_price_bucket(row[5]),
            "liquidity_change": _bucket(liq_change, BUCKETS["liquidity_change"]),
            "volume_change": _bucket(vol_change, BUCKETS["volume_change"]),
            "similarity": _bucket(sim, BUCKETS["similarity"]),
            "category": category,
            "direction": "DIP" if "DIP" in alert_type else ("OPPORTUNITY" if "OPPORTUNITY" in alert_type else "PUMP"),
        }
        result.append({
            "dims": dims,
            "strong": status == "SUCCESS",
            "continued": status in {"SUCCESS", "PARTIAL"},
            "return": normalized_training_return(row[10]),
        })
    return result


def _aggregate(rows: list[dict[str, Any]], dimensions: list[str], min_samples: int, max_order: int) -> list[dict[str, Any]]:
    accum: dict[tuple[tuple[str, str], ...], list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for row in rows:
        available = [(key, str(row["dims"][key])) for key in dimensions if row["dims"].get(key) is not None]
        for order in range(2, min(max_order, len(available)) + 1):
            for parts in combinations(available, order):
                # Avoid redundant category+direction-only segmentation: require at least one numeric/context factor.
                if all(key in {"category", "direction"} for key, _ in parts):
                    continue
                slot = accum[tuple(parts)]
                slot[0] += 1
                slot[1] += 1 if row["strong"] else 0
                slot[2] += 1 if row["continued"] else 0
                slot[3] += float(row["return"])

    output = []
    for parts, values in accum.items():
        n = int(values[0])
        if n < min_samples:
            continue
        output.append({
            "parts": list(parts),
            "samples": n,
            "strong_rate": values[1] / n * 100.0,
            "continuation_rate": values[2] / n * 100.0,
            "average_return": values[3] / n,
        })
    return output


def _reliability(samples: int) -> float:
    shrink = float(getattr(config, "COMBINATION_INTELLIGENCE_SHRINKAGE_SAMPLES", 100))
    return samples / (samples + max(1.0, shrink))


def _label(parts: list[tuple[str, str]]) -> str:
    return " + ".join(f"{LABELS.get(key, key)} {value}" for key, value in parts)


def get_combination_intelligence_report(
    checkpoint_minutes: int | None = None,
    max_rows: int | None = None,
) -> dict[str, Any]:
    checkpoint = int(checkpoint_minutes or getattr(config, "COMBINATION_INTELLIGENCE_CHECKPOINT_MINUTES", 1440))
    limit = int(max_rows or getattr(config, "COMBINATION_INTELLIGENCE_MAX_ROWS", 5000))
    min_samples = int(getattr(config, "COMBINATION_INTELLIGENCE_MIN_SAMPLES", 30))
    max_order = int(getattr(config, "COMBINATION_INTELLIGENCE_MAX_ORDER", 3))
    top_n = int(getattr(config, "COMBINATION_INTELLIGENCE_TOP_N", 10))
    dimensions = list(getattr(config, "COMBINATION_INTELLIGENCE_DIMENSIONS", tuple(LABELS.keys())))

    rows = _load_rows(checkpoint, limit)
    total = len(rows)
    if not rows:
        return {"total": 0, "shadow_mode": True, "best": [], "worst": []}

    baseline_strong = sum(row["strong"] for row in rows) / total * 100.0
    baseline_cont = sum(row["continued"] for row in rows) / total * 100.0
    baseline_return = sum(row["return"] for row in rows) / total
    combos = _aggregate(rows, dimensions, min_samples, max_order)

    for item in combos:
        reliability = _reliability(item["samples"])
        strong_delta = item["strong_rate"] - baseline_strong
        cont_delta = item["continuation_rate"] - baseline_cont
        return_delta = item["average_return"] - baseline_return
        # Continuation is primary; strong continuation and normalized return confirm it.
        raw_edge = 0.50 * cont_delta + 0.35 * strong_delta + 0.15 * return_delta
        item["strong_delta"] = strong_delta
        item["continuation_delta"] = cont_delta
        item["return_delta"] = return_delta
        item["reliability"] = reliability
        item["edge"] = raw_edge * reliability
        item["label"] = _label(item["parts"])
        item["order"] = len(item["parts"])

    def diverse(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        seen_stats: set[tuple[int, float, float, float]] = set()
        for item in items:
            fingerprint = (
                int(item["samples"]),
                round(float(item["strong_rate"]), 1),
                round(float(item["continuation_rate"]), 1),
                round(float(item["average_return"]), 1),
            )
            if fingerprint in seen_stats:
                continue
            seen_stats.add(fingerprint)
            selected.append(item)
            if len(selected) >= limit:
                break
        return selected

    best_sorted = sorted(combos, key=lambda x: (x["edge"], x["samples"]), reverse=True)
    worst_sorted = sorted(combos, key=lambda x: (x["edge"], -x["samples"]))
    best = diverse(best_sorted, top_n)
    worst = diverse(worst_sorted, min(5, top_n))

    return {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": total,
        "checkpoint_minutes": checkpoint,
        "baseline_strong": baseline_strong,
        "baseline_continuation": baseline_cont,
        "baseline_return": baseline_return,
        "combinations_evaluated": len(combos),
        "best": best,
        "worst": worst,
        "shadow_mode": True,
    }



def _alert_dimensions(alert: dict[str, Any]) -> dict[str, str | None]:
    ml = _probability(alert.get("ml_probability"))
    sim_raw = alert.get("similarity_average")
    sim = None if sim_raw is None else _number(sim_raw)
    if sim is not None and 0 <= sim <= 1:
        sim *= 100.0
    alert_type = str(alert.get("alert_type") or "").upper()
    category = str(alert.get("category") or "OTHER").upper() or "OTHER"
    return {
        "score": _bucket(_number(alert.get("score")), BUCKETS["score"]),
        "ai_quality": _bucket(_number(alert.get("ai_quality")), BUCKETS["ai_quality"]),
        "ai_risk": _bucket(_number(alert.get("ai_risk")), BUCKETS["ai_risk"]),
        "ml": _bucket(ml, BUCKETS["ml"]),
        "liquidity": _bucket(max(0.0, _number(alert.get("liquidity"))), BUCKETS["liquidity"]),
        "price_bucket": entry_price_bucket(alert.get("price") if alert.get("price") is not None else alert.get("entry_price")),
        "liquidity_change": _bucket(None if alert.get("liquidity_change_percent") is None else _number(alert.get("liquidity_change_percent")), BUCKETS["liquidity_change"]),
        "volume_change": _bucket(None if alert.get("volume_change_percent") is None else _number(alert.get("volume_change_percent")), BUCKETS["volume_change"]),
        "similarity": _bucket(sim, BUCKETS["similarity"]),
        "category": category,
        "direction": "DIP" if "DIP" in alert_type else ("OPPORTUNITY" if "OPPORTUNITY" in alert_type else "PUMP"),
    }


def calculate_combination_adjustment(alert: dict[str, Any]) -> dict[str, Any]:
    """Historical combination bonus for a *new* signal; diagnostic/shadow only.

    It is called when the signal is recorded, so later 24h outcome cannot leak into
    the candidate score. Only combinations with a substantial historical sample
    are allowed to contribute.
    """
    report = get_combination_intelligence_report()
    dims = _alert_dimensions(alert)
    min_samples = int(getattr(config, "COMBINATION_CANDIDATE_MIN_SAMPLES", 100))
    max_bonus = float(getattr(config, "COMBINATION_CANDIDATE_MAX_BONUS", 8.0))
    matches = []
    for item in report.get("best") or []:
        if int(item.get("samples") or 0) < min_samples:
            continue
        parts = item.get("parts") or []
        if all(dims.get(str(key)) == str(value) for key, value in parts):
            edge = max(0.0, float(item.get("edge") or 0.0))
            if edge > 0:
                matches.append({**item, "candidate_edge": edge})
    matches.sort(key=lambda x: (x["candidate_edge"], x.get("samples", 0)), reverse=True)
    # Use the strongest verified context, not a sum of overlapping combinations.
    raw = matches[0]["candidate_edge"] * 0.25 if matches else 0.0
    bonus = max(0.0, min(max_bonus, raw))
    return {
        "adjustment": round(bonus, 3),
        "matched": matches[:3],
        "dimensions": dims,
        "shadow_mode": True,
    }

def _stars(samples: int) -> str:
    if samples >= 300:
        n = 5
    elif samples >= 150:
        n = 4
    elif samples >= 75:
        n = 3
    elif samples >= 40:
        n = 2
    else:
        n = 1
    return "★" * n + "☆" * (5 - n)


def format_combination_intelligence_report(report: dict[str, Any]) -> str:
    total = int(report.get("total") or 0)
    if total <= 0:
        return "🧩 Combination Intelligence · Shadow Mode\n\nПока нет проверенных сигналов для анализа комбинаций."

    lines = [
        "🧩 Combination Intelligence · Shadow Mode",
        "",
        f"Проверено сигналов: {total}",
        f"Комбинаций с достаточной выборкой: {int(report.get('combinations_evaluated') or 0)}",
        "⚠️ Модуль не меняет реальные алерты.",
        "",
        "📊 Базовый уровень",
        f"Strong: {float(report.get('baseline_strong') or 0):.1f}% · Любое: {float(report.get('baseline_continuation') or 0):.1f}%",
        f"Норм. результат: {float(report.get('baseline_return') or 0):+.1f}%",
        "",
        "🏆 Лучшие комбинации",
    ]

    for index, item in enumerate(report.get("best") or [], start=1):
        lines.extend([
            f"{index}. {item['label']}",
            f"   Strong {item['strong_rate']:.1f}% ({item['strong_delta']:+.1f} п.п.) · Любое {item['continuation_rate']:.1f}% ({item['continuation_delta']:+.1f} п.п.)",
            f"   Норм. {item['average_return']:+.1f}% · n={item['samples']} {_stars(int(item['samples']))}",
        ])

    worst = report.get("worst") or []
    if worst:
        lines.extend(["", "⚠️ Слабые комбинации"])
        for index, item in enumerate(worst, start=1):
            lines.extend([
                f"{index}. {item['label']}",
                f"   Strong {item['strong_rate']:.1f}% · Любое {item['continuation_rate']:.1f}% · Норм. {item['average_return']:+.1f}% · n={item['samples']}",
            ])

    lines.extend([
        "",
        "ℹ️ Рейтинг учитывает Strong, любое продолжение, нормализованный результат и уменьшает влияние маленьких выборок.",
        "Следующий шаг — проверить устойчивость лучших комбинаций на новых данных, не меняя боевой поток.",
    ])
    return "\n".join(lines)
