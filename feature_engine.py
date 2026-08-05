"""Feature engineering for Polymarket signals.

This module is deliberately dependency-free so it runs reliably on Railway.
The rule score is informative only; it does not block alerts.
"""

from __future__ import annotations

import math
from statistics import mean, median, pstdev
from typing import Any, Optional

FEATURE_NAMES = [
    "price_logit",
    "log_liquidity",
    "days_left_scaled",
    "base_score",
    "change_5m",
    "change_15m",
    "change_1h",
    "change_24h",
    "momentum_strength",
    "trend_consistency",
    "acceleration",
    "volatility",
]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _change(signal: dict[str, Any], key: str) -> Optional[float]:
    value = signal.get(key)
    if value is None:
        return None
    return _number(value)


def calculate_features(signal: dict[str, Any]) -> dict[str, float]:
    """Return a stable numeric feature vector for rules and future ML."""
    price = _clip(_number(signal.get("price"), 0.5), 0.000001, 0.999999)
    liquidity = max(_number(signal.get("liquidity")), 0.0)
    days_left = max(_number(signal.get("days_left")), 0.0)
    base_score = _clip(_number(signal.get("score")), 0.0, 100.0)

    raw_changes = [
        _change(signal, "change_5m"),
        _change(signal, "change_15m"),
        _change(signal, "change_1h"),
        _change(signal, "change_24h"),
    ]
    known = [value for value in raw_changes if value is not None]
    filled = [value if value is not None else 0.0 for value in raw_changes]

    momentum_strength = max((abs(value) for value in known), default=0.0)
    positive = sum(1 for value in known if value > 0)
    negative = sum(1 for value in known if value < 0)
    trend_consistency = (
        max(positive, negative) / len(known)
        if known else 0.0
    )

    # Positive acceleration means the short-term move is stronger than 1h.
    acceleration = filled[0] - filled[2]
    volatility = pstdev(known) if len(known) >= 2 else 0.0

    return {
        "price_logit": math.log(price / (1.0 - price)),
        "log_liquidity": math.log1p(liquidity),
        "days_left_scaled": math.log1p(days_left),
        "base_score": base_score / 100.0,
        "change_5m": _clip(filled[0] / 100.0, -5.0, 5.0),
        "change_15m": _clip(filled[1] / 100.0, -5.0, 5.0),
        "change_1h": _clip(filled[2] / 100.0, -5.0, 5.0),
        "change_24h": _clip(filled[3] / 100.0, -5.0, 5.0),
        "momentum_strength": _clip(momentum_strength / 100.0, 0.0, 5.0),
        "trend_consistency": trend_consistency,
        "acceleration": _clip(acceleration / 100.0, -5.0, 5.0),
        "volatility": _clip(volatility / 100.0, 0.0, 5.0),
    }


def calculate_rule_assessment(signal: dict[str, Any]) -> dict[str, Any]:
    """Produce a transparent 0-100 quality/risk estimate.

    This is a deterministic baseline, not a trained probability.
    """
    features = calculate_features(signal)
    price = _number(signal.get("price"))
    liquidity = _number(signal.get("liquidity"))
    known_changes = [
        value for value in (
            _change(signal, "change_5m"),
            _change(signal, "change_15m"),
            _change(signal, "change_1h"),
            _change(signal, "change_24h"),
        ) if value is not None
    ]

    liquidity_score = _clip((math.log10(max(liquidity, 10.0)) - 1.0) * 18.0, 0.0, 100.0)
    consistency_score = features["trend_consistency"] * 100.0
    base_score = features["base_score"] * 100.0
    history_score = min(len(known_changes) / 4.0, 1.0) * 100.0

    volatility_penalty = _clip(features["volatility"] * 35.0, 0.0, 45.0)
    micro_price_penalty = 12.0 if price <= 0.003 else 0.0
    illiquid_penalty = 20.0 if liquidity < 1_000 else 0.0

    quality = (
        0.35 * base_score
        + 0.30 * liquidity_score
        + 0.20 * consistency_score
        + 0.15 * history_score
        - volatility_penalty
        - micro_price_penalty
        - illiquid_penalty
    )
    quality = int(round(_clip(quality, 0.0, 100.0)))

    risk = int(round(_clip(
        100.0
        - 0.45 * liquidity_score
        - 0.25 * history_score
        - 0.15 * consistency_score
        + volatility_penalty
        + micro_price_penalty
        + illiquid_penalty,
        0.0,
        100.0,
    )))

    reasons: list[str] = []
    if liquidity >= 500_000:
        reasons.append("высокая ликвидность")
    elif liquidity < 10_000:
        reasons.append("низкая ликвидность")
    if features["trend_consistency"] >= 0.75 and known_changes:
        reasons.append("движение подтверждается периодами")
    if features["volatility"] >= 0.8:
        reasons.append("повышенная волатильность")
    if len(known_changes) < 2:
        reasons.append("мало истории")

    return {
        "ai_quality": quality,
        "ai_risk": risk,
        "feature_count": len(FEATURE_NAMES),
        "reasons": reasons,
        "features": features,
    }


def vector_from_features(features: dict[str, float]) -> list[float]:
    return [_number(features.get(name)) for name in FEATURE_NAMES]


# ---------------- FEATURE IMPORTANCE / AI INSIGHTS ----------------

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass


@dataclass(frozen=True)
class _Bucket:
    label: str
    low: float | None = None
    high: float | None = None

    def contains(self, value: float) -> bool:
        if self.low is not None and value < self.low:
            return False
        if self.high is not None and value >= self.high:
            return False
        return True


_NUMERIC_BUCKETS: dict[str, tuple[str, tuple[_Bucket, ...]]] = {
    "score": ("Score", (
        _Bucket("<40", None, 40), _Bucket("40–59", 40, 60),
        _Bucket("60–74", 60, 75), _Bucket("75–84", 75, 85),
        _Bucket("85+", 85, None),
    )),
    "ai_quality": ("AI Quality", (
        _Bucket("<40", None, 40), _Bucket("40–59", 40, 60),
        _Bucket("60–74", 60, 75), _Bucket("75–84", 75, 85),
        _Bucket("85+", 85, None),
    )),
    "ai_risk": ("AI Risk", (
        _Bucket("0–19", None, 20), _Bucket("20–39", 20, 40),
        _Bucket("40–59", 40, 60), _Bucket("60+", 60, None),
    )),
    "ml": ("ML", (
        _Bucket("<10%", None, 10), _Bucket("10–24%", 10, 25),
        _Bucket("25–39%", 25, 40), _Bucket("40–59%", 40, 60),
        _Bucket("60%+", 60, None),
    )),
    "liquidity": ("Ликвидность", (
        _Bucket("<$10k", None, 10_000), _Bucket("$10–50k", 10_000, 50_000),
        _Bucket("$50–250k", 50_000, 250_000),
        _Bucket("$250k–1M", 250_000, 1_000_000),
        _Bucket("$1M+", 1_000_000, None),
    )),
    "price_change": ("Движение цены", (
        _Bucket("<5%", None, 5), _Bucket("5–14%", 5, 15),
        _Bucket("15–29%", 15, 30), _Bucket("30–59%", 30, 60),
        _Bucket("60%+", 60, None),
    )),
    "volume_change": ("Изм. объёма", (
        _Bucket("<-20%", None, -20), _Bucket("-20–0%", -20, 0),
        _Bucket("0–20%", 0, 20), _Bucket("20–80%", 20, 80),
        _Bucket("80%+", 80, None),
    )),
    "liquidity_change": ("Изм. ликвидности", (
        _Bucket("<-10%", None, -10), _Bucket("-10–0%", -10, 0),
        _Bucket("0–10%", 0, 10), _Bucket("10–30%", 10, 30),
        _Bucket("30%+", 30, None),
    )),
    "similarity": ("Similarity", (
        _Bucket("<60%", None, 60), _Bucket("60–69%", 60, 70),
        _Bucket("70–79%", 70, 80), _Bucket("80–89%", 80, 90),
        _Bucket("90%+", 90, None),
    )),
}


def _json_object(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _probability_percent_value(value: Any) -> float | None:
    if value is None:
        return None
    number = _number(value, float("nan"))
    if not math.isfinite(number):
        return None
    if 0 <= number <= 1:
        number *= 100
    return _clip(number, 0, 100)


def _insight_rows(checkpoint_minutes: int, max_rows: int) -> list[dict[str, Any]]:
    # Local import prevents a circular dependency during ai_engine startup.
    from database import get_connection

    try:
        with closing(get_connection()) as connection:
            rows = connection.execute(
                """
                SELECT s.base_score, s.ai_quality, s.ai_risk, s.ml_probability,
                       s.liquidity, s.category, s.alert_type, s.metadata_json,
                       o.status, o.directional_return_percent
                FROM ai_signals s
                JOIN signal_outcomes o ON o.signal_id = s.signal_id
                WHERE o.checkpoint_minutes = ? AND o.status IS NOT NULL
                ORDER BY o.measured_at DESC
                LIMIT ?
                """,
                (int(checkpoint_minutes), int(max_rows)),
            ).fetchall()
    except sqlite3.OperationalError:
        return []

    result: list[dict[str, Any]] = []
    for row in rows:
        metadata = _json_object(row[7])
        status = str(row[8] or "").upper()
        result.append({
            "score": _number(row[0]),
            "ai_quality": _number(row[1]),
            "ai_risk": _number(row[2]),
            "ml": _probability_percent_value(row[3]),
            "liquidity": max(0.0, _number(row[4])),
            "category": str(row[5] or "OTHER").upper(),
            "direction": "DIP" if "DIP" in str(row[6] or "").upper() else "PUMP",
            "price_change": abs(_number(metadata.get("price_change_percent"))),
            "volume_change": (
                None if metadata.get("volume_change_percent") is None
                else _number(metadata.get("volume_change_percent"))
            ),
            "liquidity_change": (
                None if metadata.get("liquidity_change_percent") is None
                else _number(metadata.get("liquidity_change_percent"))
            ),
            "similarity": (
                None if metadata.get("similarity_average") is None
                else _number(metadata.get("similarity_average"))
            ),
            "status": status,
            "strong": status == "SUCCESS",
            "continued": status in {"SUCCESS", "PARTIAL"},
            "return": _number(row[9]),
        })
    return result


def _bucket_statistics(
    rows: list[dict[str, Any]],
    key: str,
    buckets: tuple[_Bucket, ...],
    min_samples: int,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for bucket in buckets:
        selected = [
            row for row in rows
            if row.get(key) is not None and bucket.contains(float(row[key]))
        ]
        count = len(selected)
        if count < min_samples:
            continue
        output.append({
            "label": bucket.label,
            "samples": count,
            "strong_rate": sum(row["strong"] for row in selected) / count * 100,
            "continuation_rate": sum(row["continued"] for row in selected) / count * 100,
            "average_return": sum(row["return"] for row in selected) / count,
        })
    return output


def _importance_from_buckets(stats: list[dict[str, Any]], total_rows: int) -> float:
    if len(stats) < 2 or total_rows <= 0:
        return 0.0
    rates = [item["continuation_rate"] for item in stats]
    coverage = min(1.0, sum(item["samples"] for item in stats) / total_rows)
    reliability = min(1.0, sum(item["samples"] for item in stats) / 300.0)
    # A transparent 0..100 diagnostic score, not a causal importance measure.
    return round((max(rates) - min(rates)) * coverage * reliability, 1)


def _category_statistics(
    rows: list[dict[str, Any]],
    min_samples: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("category") or "OTHER"), []).append(row)
    output: list[dict[str, Any]] = []
    for category, selected in grouped.items():
        count = len(selected)
        if count < min_samples:
            continue
        output.append({
            "label": category,
            "samples": count,
            "strong_rate": sum(row["strong"] for row in selected) / count * 100,
            "continuation_rate": sum(row["continued"] for row in selected) / count * 100,
            "average_return": sum(row["return"] for row in selected) / count,
        })
    return sorted(output, key=lambda item: item["continuation_rate"], reverse=True)


def _trimmed_mean_values(values: list[float], trim_fraction: float = 0.05) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    trim = int(len(ordered) * max(0.0, min(float(trim_fraction), 0.20)))
    if trim > 0 and len(ordered) - 2 * trim >= 1:
        ordered = ordered[trim:-trim]
    return sum(ordered) / len(ordered)


def _reliability(samples: int) -> dict[str, Any]:
    count = max(0, int(samples))
    if count >= 300:
        stars, label = 5, "очень высокая"
    elif count >= 150:
        stars, label = 4, "высокая"
    elif count >= 75:
        stars, label = 3, "средняя"
    elif count >= 30:
        stars, label = 2, "низкая"
    else:
        stars, label = 1, "предварительная"
    return {
        "stars": stars,
        "stars_text": "★" * stars + "☆" * (5 - stars),
        "label": label,
    }


def get_feature_importance_report(
    checkpoint_minutes: int = 1440,
    max_rows: int = 5000,
    min_bucket_samples: int = 20,
) -> dict[str, Any]:
    """Analyze which stored signal factors separate outcomes most clearly.

    The returned importance score is descriptive. It measures historical
    separation between buckets and must not be interpreted as causality.
    """
    rows = _insight_rows(checkpoint_minutes, max_rows)
    total = len(rows)
    if not rows:
        return {"total": 0, "factors": [], "categories": []}

    factors: list[dict[str, Any]] = []
    for key, (label, buckets) in _NUMERIC_BUCKETS.items():
        stats = _bucket_statistics(rows, key, buckets, min_bucket_samples)
        if len(stats) < 2:
            continue
        best = max(stats, key=lambda item: (item["continuation_rate"], item["average_return"]))
        worst = min(stats, key=lambda item: (item["continuation_rate"], item["average_return"]))
        factors.append({
            "key": key,
            "label": label,
            "importance": _importance_from_buckets(stats, total),
            "best": {**best, "reliability": _reliability(best["samples"])},
            "worst": {**worst, "reliability": _reliability(worst["samples"])},
            "buckets": [
                {**item, "reliability": _reliability(item["samples"])}
                for item in stats
            ],
        })
    factors.sort(key=lambda item: item["importance"], reverse=True)

    categories = _category_statistics(rows, min_bucket_samples)
    return {
        "total": total,
        "checkpoint_minutes": checkpoint_minutes,
        "strong_rate": sum(row["strong"] for row in rows) / total * 100,
        "continuation_rate": sum(row["continued"] for row in rows) / total * 100,
        "average_return": sum(row["return"] for row in rows) / total,
        "median_return": median([row["return"] for row in rows]),
        "trimmed_mean_return": _trimmed_mean_values([row["return"] for row in rows], 0.05),
        "mean_absolute_return": sum(abs(row["return"]) for row in rows) / total,
        "return_stddev": pstdev([row["return"] for row in rows]) if total >= 2 else 0.0,
        "factors": factors,
        "categories": categories,
        "similarity_samples": sum(row.get("similarity") is not None for row in rows),
    }


def format_feature_importance_report(report: dict[str, Any]) -> str:
    total = int(report.get("total") or 0)
    if total == 0:
        return "🧠 AI Insights\n\nПока нет проверенных сигналов для анализа."

    lines = [
        "🧠 AI Insights",
        "",
        f"Проверено сигналов: {total}",
        f"Сильное продолжение: {float(report['strong_rate']):.1f}%",
        f"Любое продолжение: {float(report['continuation_rate']):.1f}%",
        f"Средний результат: {float(report['average_return']):+.1f}%",
        f"Медиана: {float(report['median_return']):+.1f}%",
        f"Обрезанное среднее (5%): {float(report['trimmed_mean_return']):+.1f}%",
        f"Среднее |движение|: {float(report['mean_absolute_return']):.1f}%",
        f"Стандартное отклонение: {float(report['return_stddev']):.1f}%",
        "",
        "📊 Историческая эффективность факторов",
    ]

    factors = list(report.get("factors") or [])
    if not factors:
        lines.append("Недостаточно данных по диапазонам.")
    else:
        for index, factor in enumerate(factors[:7], start=1):
            best = factor["best"]
            lines.append(
                f"{index}. {factor['label']} — {factor['importance']:.1f}/100\n"
                f"   Лучший диапазон: {best['label']} · "
                f"{best['continuation_rate']:.1f}% продолжений "
                f"(n={best['samples']})\n"
                f"   Надёжность: {best['reliability']['stars_text']} "
                f"({best['reliability']['label']})"
            )

    similarity_samples = int(report.get("similarity_samples") or 0)
    if similarity_samples < 50:
        lines.extend([
            "",
            f"ℹ️ Similarity сохранён у {similarity_samples} сигналов.",
            "Его оценка станет надёжнее после накопления новых проверок.",
        ])

    categories = list(report.get("categories") or [])
    if categories:
        lines.extend(["", "🏷 Категории"])
        for item in categories[:5]:
            lines.append(
                f"• {item['label']}: {item['continuation_rate']:.1f}% "
                f"(n={item['samples']})"
            )

    lines.extend([
        "",
        "⚠️ Это историческая связь, а не гарантия и не причинный анализ.",
    ])
    return "\n".join(lines)
