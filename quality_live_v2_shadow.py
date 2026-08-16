"""Quality Live v2 Shadow: strict future-only quality experiment.

This module NEVER filters or sends alerts. It only records whether each newly
recorded signal would pass the v2 rules and compares 24h outcomes later.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from database import get_connection
from result_normalization import normalized_training_return


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema() -> None:
    with closing(get_connection()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS quality_live_v2_shadow (
            signal_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            selected INTEGER NOT NULL,
            reason TEXT,
            confirmations INTEGER NOT NULL,
            score REAL,
            category TEXT,
            entry_price REAL,
            similarity REAL,
            liquidity_change REAL
        );
        CREATE INDEX IF NOT EXISTS idx_quality_live_v2_created
        ON quality_live_v2_shadow(created_at);
        """)
        connection.commit()


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def evaluate(alert: dict[str, Any]) -> tuple[bool, str, int, dict[str, Any]]:
    """Strict v2 rule derived before outcomes; no historical lookup here."""
    score = float(alert.get("score") or 0.0)
    alert_type = str(alert.get("alert_type") or "").upper()
    category = str(alert.get("category") or "OTHER").upper()
    price = _num(alert.get("current_price"))
    if price is None:
        price = _num(alert.get("price"))
    similarity = _num(alert.get("similarity_average"))
    if similarity is not None and 0 <= similarity <= 1:
        similarity *= 100.0
    liq_change = _num(alert.get("liquidity_change_percent"))

    confirmations = 0
    confirmations += int(price is not None and 0.01 <= price < 0.05)
    confirmations += int(similarity is not None and 80 <= similarity < 90)
    confirmations += int(liq_change is not None and liq_change >= 30)

    meta = {
        "score": score, "category": category, "entry_price": price,
        "similarity": similarity, "liquidity_change": liq_change,
    }
    if "OPPORTUNITY" in alert_type:
        return False, "OPPORTUNITY", confirmations, meta
    if not (60 <= score <= 74):
        return False, "SCORE_BUCKET", confirmations, meta
    if "AI/TECH" not in category:
        return False, "NOT_AI_TECH", confirmations, meta
    if confirmations < 1:
        return False, "NO_CONFIRMATION", confirmations, meta
    return True, "SELECTED", confirmations, meta


def record_shadow_decision(alert: dict[str, Any]) -> None:
    signal_id = str(alert.get("ai_signal_id") or "")
    if not signal_id:
        return
    selected, reason, confirmations, meta = evaluate(alert)
    ensure_schema()
    with closing(get_connection()) as connection:
        connection.execute(
            """INSERT OR IGNORE INTO quality_live_v2_shadow
               (signal_id, created_at, selected, reason, confirmations, score,
                category, entry_price, similarity, liquidity_change)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (signal_id, _now(), int(selected), reason, confirmations,
             meta["score"], meta["category"], meta["entry_price"],
             meta["similarity"], meta["liquidity_change"]),
        )
        connection.commit()


def _stats(rows: list[tuple[Any, Any]]) -> dict[str, Any]:
    n = len(rows)
    strong = sum(str(status).upper() == "SUCCESS" for status, _ in rows)
    continued = sum(str(status).upper() in {"SUCCESS", "PARTIAL"} for status, _ in rows)
    values = [normalized_training_return(float(ret)) for _, ret in rows if ret is not None]
    return {
        "n": n,
        "strong": strong / n * 100 if n else None,
        "continued": continued / n * 100 if n else None,
        "normalized": sum(values) / len(values) if values else None,
    }


def get_report(checkpoint_minutes: int = 1440) -> dict[str, Any]:
    ensure_schema()
    with closing(get_connection()) as connection:
        counts = connection.execute(
            """SELECT COUNT(*), SUM(selected),
                      SUM(CASE WHEN selected=0 THEN 1 ELSE 0 END)
               FROM quality_live_v2_shadow"""
        ).fetchone()
        selected_rows = connection.execute(
            """SELECT o.status, o.directional_return_percent
               FROM quality_live_v2_shadow q
               JOIN signal_outcomes o ON o.signal_id=q.signal_id
               WHERE q.selected=1 AND o.checkpoint_minutes=? AND o.status IS NOT NULL""",
            (checkpoint_minutes,),
        ).fetchall()
        rejected_rows = connection.execute(
            """SELECT o.status, o.directional_return_percent
               FROM quality_live_v2_shadow q
               JOIN signal_outcomes o ON o.signal_id=q.signal_id
               WHERE q.selected=0 AND o.checkpoint_minutes=? AND o.status IS NOT NULL""",
            (checkpoint_minutes,),
        ).fetchall()
        reasons = dict(connection.execute(
            """SELECT reason, COUNT(*) FROM quality_live_v2_shadow
               WHERE selected=0 GROUP BY reason ORDER BY COUNT(*) DESC"""
        ).fetchall())
    return {
        "total": int(counts[0] or 0), "selected": int(counts[1] or 0),
        "rejected": int(counts[2] or 0), "selected_stats": _stats(selected_rows),
        "rejected_stats": _stats(rejected_rows), "reasons": reasons,
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def format_report(report: dict[str, Any]) -> str:
    a, b = report["selected_stats"], report["rejected_stats"]
    lines = [
        "🧪 Quality Live v2 · Shadow", "",
        "⚠️ Не влияет на реальные алерты.",
        "Правило: Score 60–74 + AI/TECH + ≥1 подтверждение",
        "(цена 1–5¢ / Similarity 80–89% / Δ ликвидности 30%+).", "",
        f"Новых сигналов после установки: {report['total']}",
        f"Выбрано v2: {report['selected']}",
        f"Отклонено v2: {report['rejected']}", "",
        f"✅ Проверено через 24ч: {a['n']} выбранных / {b['n']} отклонённых", "",
        "🏆 V2 выбранные:",
        f"Strong: {_pct(a['strong'])}", f"Любое: {_pct(a['continued'])}",
        f"Норм.: {_pct(a['normalized'])}", "",
        "🚫 V2 отклонённые:",
        f"Strong: {_pct(b['strong'])}", f"Любое: {_pct(b['continued'])}",
        f"Норм.: {_pct(b['normalized'])}",
    ]
    if a["strong"] is not None and b["strong"] is not None:
        lines += ["", f"📈 Преимущество v2 Strong: {a['strong'] - b['strong']:+.1f} п.п."]
    if report["reasons"]:
        lines += ["", "Причины отклонения:"]
        labels = {"OPPORTUNITY":"OPPORTUNITY", "SCORE_BUCKET":"Score вне 60–74",
                  "NOT_AI_TECH":"Не AI/TECH", "NO_CONFIRMATION":"Нет подтверждающего фактора"}
        for key, value in report["reasons"].items():
            lines.append(f"• {labels.get(key, key)}: {value}")
    lines += ["", "ℹ️ Future-only: старая история в этот тест не подмешивается."]
    return "\n".join(lines)
