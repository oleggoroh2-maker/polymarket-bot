"""Quality Live Mode analytics: future-only audit of sent vs filtered signals."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import config
from database import get_connection
from result_normalization import normalized_training_return


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_quality_live_schema() -> None:
    with closing(get_connection()) as connection:
        connection.executescript("""
        CREATE TABLE IF NOT EXISTS quality_live_events (
            signal_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT,
            score REAL,
            alert_type TEXT,
            sent_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_quality_live_events_created
        ON quality_live_events(created_at);
        """)
        connection.commit()


def record_quality_decision(alert: dict[str, Any], passed: bool, reason: str | None = None) -> None:
    signal_id = str(alert.get("ai_signal_id") or "")
    if not signal_id:
        return
    ensure_quality_live_schema()
    with closing(get_connection()) as connection:
        connection.execute(
            """INSERT OR REPLACE INTO quality_live_events
               (signal_id, created_at, decision, reason, score, alert_type, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, COALESCE((SELECT sent_at FROM quality_live_events WHERE signal_id=?), NULL))""",
            (signal_id, _now(), "ELIGIBLE" if passed else "FILTERED", reason,
             float(alert.get("score") or 0), str(alert.get("alert_type") or ""), signal_id),
        )
        connection.commit()


def mark_quality_sent(signal_id: str | None) -> None:
    if not signal_id:
        return
    ensure_quality_live_schema()
    with closing(get_connection()) as connection:
        connection.execute(
            "UPDATE quality_live_events SET decision='SENT', sent_at=? WHERE signal_id=? AND decision!='FILTERED'",
            (_now(), str(signal_id)),
        )
        connection.commit()


def _group_stats(rows: list[tuple[Any, Any]]) -> dict[str, Any]:
    total = len(rows)
    strong = sum(1 for status, _ in rows if str(status).upper() == "SUCCESS")
    continued = sum(1 for status, _ in rows if str(status).upper() in {"SUCCESS", "PARTIAL"})
    values = [normalized_training_return(float(ret)) for _, ret in rows if ret is not None]
    return {
        "n": total,
        "strong": strong / total * 100.0 if total else None,
        "continued": continued / total * 100.0 if total else None,
        "normalized": sum(values) / len(values) if values else None,
    }


def _decision_sample(connection, decision: str, checkpoint_minutes: int, limit: int = 3) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT q.created_at, q.reason, q.score, q.alert_type,
               s.title, s.base_score, s.category, s.entry_price,
               o.status, o.directional_return_percent
        FROM quality_live_events q
        JOIN ai_signals s ON s.signal_id=q.signal_id
        LEFT JOIN signal_outcomes o
          ON o.signal_id=q.signal_id AND o.checkpoint_minutes=?
        WHERE q.decision=?
        ORDER BY q.created_at DESC
        LIMIT ?
        """,
        (int(checkpoint_minutes), str(decision), int(limit)),
    ).fetchall()
    result = []
    for row in rows:
        result.append({
            "created_at": row[0], "reason": row[1], "recorded_score": row[2],
            "alert_type": row[3], "title": row[4], "base_score": row[5],
            "category": row[6], "entry_price": row[7], "status": row[8],
            "directional_return": row[9],
        })
    return result


def get_quality_live_report(checkpoint_minutes: int = 1440) -> dict[str, Any]:
    ensure_quality_live_schema()
    score_min = float(getattr(config, "QUALITY_LIVE_SCORE_MIN", 60))
    score_max = float(getattr(config, "QUALITY_LIVE_SCORE_MAX", 74))
    with closing(get_connection()) as connection:
        counts = connection.execute("""
            SELECT COUNT(*),
                   SUM(CASE WHEN decision='SENT' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN decision='FILTERED' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN decision='ELIGIBLE' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN decision='FILTERED' AND reason='SCORE_BUCKET' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN decision='FILTERED' AND reason='OPPORTUNITY' THEN 1 ELSE 0 END)
            FROM quality_live_events
        """).fetchone()
        sent_rows = connection.execute("""
            SELECT o.status, o.directional_return_percent
            FROM quality_live_events q
            JOIN signal_outcomes o ON o.signal_id=q.signal_id
            WHERE q.decision='SENT' AND o.checkpoint_minutes=? AND o.status IS NOT NULL
        """, (checkpoint_minutes,)).fetchall()
        filtered_rows = connection.execute("""
            SELECT o.status, o.directional_return_percent
            FROM quality_live_events q
            JOIN signal_outcomes o ON o.signal_id=q.signal_id
            WHERE q.decision='FILTERED' AND o.checkpoint_minutes=? AND o.status IS NOT NULL
        """, (checkpoint_minutes,)).fetchall()

        # Integrity audit: these values should all be zero in a correct deployment.
        audit = connection.execute("""
            SELECT
              SUM(CASE WHEN q.decision='SENT' AND (s.base_score < ? OR s.base_score > ?) THEN 1 ELSE 0 END),
              SUM(CASE WHEN q.decision='FILTERED' AND q.reason='SCORE_BUCKET' AND s.base_score BETWEEN ? AND ? THEN 1 ELSE 0 END),
              SUM(CASE WHEN q.decision='SENT' AND UPPER(s.alert_type) LIKE '%OPPORTUNITY%' THEN 1 ELSE 0 END),
              SUM(CASE WHEN ABS(COALESCE(q.score, 0) - COALESCE(s.base_score, 0)) > 0.001 THEN 1 ELSE 0 END)
            FROM quality_live_events q
            JOIN ai_signals s ON s.signal_id=q.signal_id
        """, (score_min, score_max, score_min, score_max)).fetchone()

        sent_sample = _decision_sample(connection, "SENT", checkpoint_minutes, 3)
        filtered_sample = _decision_sample(connection, "FILTERED", checkpoint_minutes, 3)

    return {
        "candidates": int(counts[0] or 0), "sent": int(counts[1] or 0),
        "filtered": int(counts[2] or 0), "eligible": int(counts[3] or 0),
        "score_filtered": int(counts[4] or 0), "opportunity_filtered": int(counts[5] or 0),
        "sent_stats": _group_stats(sent_rows), "filtered_stats": _group_stats(filtered_rows),
        "audit": {
            "sent_outside_score": int(audit[0] or 0),
            "filtered_inside_score": int(audit[1] or 0),
            "opportunity_sent": int(audit[2] or 0),
            "score_mismatch": int(audit[3] or 0),
        },
        "sent_sample": sent_sample,
        "filtered_sample": filtered_sample,
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def _compact_title(value: Any, limit: int = 52) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _sample_line(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "WAIT").upper()
    icon = {"SUCCESS": "✅", "PARTIAL": "🟡", "NEUTRAL": "⚪", "FAIL": "❌"}.get(status, "⏳")
    ret = item.get("directional_return")
    ret_text = "ждёт 24ч" if ret is None else f"{float(ret):+.1f}%"
    reason = f" · {item.get('reason')}" if item.get("reason") else ""
    return f"{icon} S{float(item.get('base_score') or 0):.0f} · {ret_text}{reason}\n{_compact_title(item.get('title'))}"


def format_quality_live_report(report: dict[str, Any]) -> str:
    sent = report["sent_stats"]
    filtered = report["filtered_stats"]
    audit = report.get("audit") or {}
    integrity_total = sum(int(value or 0) for value in audit.values())
    lines = [
        "🟢 Quality Live Mode · 24ч", "",
        f"Найдено кандидатов: {report['candidates']}",
        f"Отправлено: {report['sent']}",
        f"Отфильтровано: {report['filtered']}",
    ]
    if report["eligible"]:
        lines.append(f"Ожидают/не доставлены: {report['eligible']}")
    lines += ["", "Причины фильтрации:",
              f"• Score вне {getattr(config, 'QUALITY_LIVE_SCORE_MIN', 60):g}–{getattr(config, 'QUALITY_LIVE_SCORE_MAX', 74):g}: {report['score_filtered']}",
              f"• OPPORTUNITY: {report['opportunity_filtered']}", "",
              f"✅ Проверено через 24ч: {sent['n']} отправленных / {filtered['n']} отфильтрованных", "",
              "📨 Отправленные:",
              f"Strong: {_pct(sent['strong'])}", f"Любое: {_pct(sent['continued'])}", f"Норм.: {_pct(sent['normalized'])}", "",
              "🚫 Отфильтрованные:",
              f"Strong: {_pct(filtered['strong'])}", f"Любое: {_pct(filtered['continued'])}", f"Норм.: {_pct(filtered['normalized'])}"]
    if sent["strong"] is not None and filtered["strong"] is not None:
        lines += ["", f"📈 Преимущество Live Strong: {sent['strong'] - filtered['strong']:+.1f} п.п."]

    lines += ["", "🔎 Quality Live Audit"]
    if integrity_total == 0:
        lines.append("✅ Маршрутизация корректна: несоответствий не найдено.")
    else:
        lines += [
            "⚠️ Найдены несоответствия:",
            f"• SENT вне Score-диапазона: {audit.get('sent_outside_score', 0)}",
            f"• SCORE_BUCKET внутри диапазона: {audit.get('filtered_inside_score', 0)}",
            f"• OPPORTUNITY реально отправлены: {audit.get('opportunity_sent', 0)}",
            f"• Score решения ≠ Score AI Memory: {audit.get('score_mismatch', 0)}",
        ]

    if report.get("sent_sample"):
        lines += ["", "📨 Последние отправленные"]
        for index, item in enumerate(report["sent_sample"], 1):
            lines.append(f"{index}. {_sample_line(item)}")
    if report.get("filtered_sample"):
        lines += ["", "🚫 Последние отфильтрованные"]
        for index, item in enumerate(report["filtered_sample"], 1):
            lines.append(f"{index}. {_sample_line(item)}")

    lines += ["", "ℹ️ Audit проверяет, что Live использует тот же Score, что AI Memory, не пропускает OPPORTUNITY и корректно связывает 24ч исход с решением."]
    return "\n".join(lines)

