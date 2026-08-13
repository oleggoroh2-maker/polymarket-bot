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


def get_quality_live_report(checkpoint_minutes: int = 1440) -> dict[str, Any]:
    ensure_quality_live_schema()
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
    return {
        "candidates": int(counts[0] or 0), "sent": int(counts[1] or 0),
        "filtered": int(counts[2] or 0), "eligible": int(counts[3] or 0),
        "score_filtered": int(counts[4] or 0), "opportunity_filtered": int(counts[5] or 0),
        "sent_stats": _group_stats(sent_rows), "filtered_stats": _group_stats(filtered_rows),
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}%"


def format_quality_live_report(report: dict[str, Any]) -> str:
    sent = report["sent_stats"]
    filtered = report["filtered_stats"]
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
    lines += ["", "ℹ️ Учёт начался с момента установки этого модуля; старая история намеренно не подмешивается."]
    return "\n".join(lines)
