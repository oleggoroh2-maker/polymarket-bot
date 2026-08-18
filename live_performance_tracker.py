"""Live Performance Tracker for actually delivered Quality Engine v3 alerts."""
from __future__ import annotations
from contextlib import closing
from typing import Any
from database import get_connection
from quality_live_analytics import ensure_quality_live_schema
from result_normalization import normalized_training_return

CHECKPOINTS = ((60, "1ч"), (360, "6ч"), (1440, "24ч"))


def _stats(rows):
    n=len(rows); strong=sum(str(r[0]).upper()=="SUCCESS" for r in rows)
    cont=sum(str(r[0]).upper() in {"SUCCESS","PARTIAL"} for r in rows)
    vals=[normalized_training_return(float(r[1])) for r in rows if r[1] is not None]
    fail=sum(str(r[0]).upper()=="FAIL" for r in rows)
    return {"n":n,"strong":strong/n*100 if n else None,"continued":cont/n*100 if n else None,
            "failed":fail/n*100 if n else None,"normalized":sum(vals)/len(vals) if vals else None}


def get_live_performance_report() -> dict[str, Any]:
    ensure_quality_live_schema()
    with closing(get_connection()) as c:
        total=int(c.execute("SELECT COUNT(*) FROM quality_live_events WHERE engine_version='v3'").fetchone()[0] or 0)
        sent=int(c.execute("SELECT COUNT(*) FROM quality_live_events WHERE engine_version='v3' AND decision='SENT'").fetchone()[0] or 0)
        filtered=int(c.execute("SELECT COUNT(*) FROM quality_live_events WHERE engine_version='v3' AND decision='FILTERED'").fetchone()[0] or 0)
        cps={}
        for cp,label in CHECKPOINTS:
            rows=c.execute("""SELECT o.status,o.directional_return_percent FROM quality_live_events q
                JOIN signal_outcomes o ON o.signal_id=q.signal_id
                WHERE q.engine_version='v3' AND q.decision='SENT' AND o.checkpoint_minutes=? AND o.status IS NOT NULL""",(cp,)).fetchall()
            cps[label]=_stats(rows)
        cats=[]
        rows=c.execute("""SELECT COALESCE(s.category,'OTHER'), COUNT(*) FROM quality_live_events q
            JOIN ai_signals s ON s.signal_id=q.signal_id
            WHERE q.engine_version='v3' AND q.decision='SENT' GROUP BY COALESCE(s.category,'OTHER') ORDER BY COUNT(*) DESC""").fetchall()
        for cat,count in rows:
            outcomes=c.execute("""SELECT o.status,o.directional_return_percent FROM quality_live_events q
                JOIN ai_signals s ON s.signal_id=q.signal_id JOIN signal_outcomes o ON o.signal_id=q.signal_id
                WHERE q.engine_version='v3' AND q.decision='SENT' AND COALESCE(s.category,'OTHER')=?
                  AND o.checkpoint_minutes=1440 AND o.status IS NOT NULL""",(cat,)).fetchall()
            cats.append((str(cat),int(count),_stats(outcomes)))
        recent=c.execute("""SELECT s.category,s.title,s.base_score,q.reason,o.status,o.directional_return_percent
            FROM quality_live_events q JOIN ai_signals s ON s.signal_id=q.signal_id
            LEFT JOIN signal_outcomes o ON o.signal_id=q.signal_id AND o.checkpoint_minutes=1440
            WHERE q.engine_version='v3' AND q.decision='SENT' ORDER BY COALESCE(q.sent_at,q.created_at) DESC LIMIT 5""").fetchall()
    return {"total":total,"sent":sent,"filtered":filtered,"checkpoints":cps,"categories":cats,"recent":recent}


def _pct(v): return "—" if v is None else f"{v:.1f}%"
def _title(v,n=48):
    t=" ".join(str(v or "").split()); return t if len(t)<=n else t[:n-1]+"…"

def format_live_performance_report(r):
    lines=["🔥 Quality Engine v3 · LIVE PERFORMANCE","",f"Решений V3: {r['total']}",f"Реально отправлено: {r['sent']}",f"Отфильтровано V3: {r['filtered']}","","⏱ Результат отправленных"]
    for label in ("1ч","6ч","24ч"):
        x=r['checkpoints'][label]
        lines.append(f"• {label}: n={x['n']} · Strong {_pct(x['strong'])} · Любое {_pct(x['continued'])} · Норм. {_pct(x['normalized'])}")
    lines += ["","🏷 24ч по категориям"]
    if not r['categories']: lines.append("• пока нет отправленных")
    for cat,count,x in r['categories']:
        lines.append(f"• {cat}: отправлено {count} · проверено {x['n']} · Strong {_pct(x['strong'])} · Норм. {_pct(x['normalized'])}")
    lines += ["","📨 Последние боевые сигналы"]
    if not r['recent']: lines.append("• пока нет")
    for cat,title,score,reason,status,ret in r['recent']:
        icon={"SUCCESS":"✅","PARTIAL":"🟡","NEUTRAL":"⚪","FAIL":"❌"}.get(str(status or '').upper(),"⏳")
        result="ждёт 24ч" if ret is None else f"{float(ret):+.1f}%"
        lines.append(f"{icon} {cat} · S{float(score or 0):.0f} · {result}\n{_title(title)}")
    lines += ["","ℹ️ Здесь учитываются только решения, созданные Quality Engine v3 после этого обновления; старая история не подмешивается."]
    return "\n".join(lines)
