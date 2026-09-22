"""Pilot Engine Audit v1 — read-only TRADE vs MAX_OPEN counterfactual audit.

Does not change entry rules or historical decisions. It compares identical future
outcomes for accepted PAPER trades and candidates skipped only because the five-slot
concurrency limit was full, and audits the concurrency accounting itself.
"""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone, timedelta
import math
from statistics import median
from database import get_connection
from pilot_engine_v1 import VERSION, HORIZON_MINUTES

CHECKPOINTS=(180,360,720,1440)


def _stats(vals):
    vals=[float(x) for x in vals]
    if not vals:return {'n':0,'roi':None,'pf':None,'win':None,'median':None}
    gp=sum(max(x,0) for x in vals); gl=-sum(min(x,0) for x in vals)
    return {'n':len(vals),'roi':sum(vals)/len(vals),'pf':gp/gl if gl else (float('inf') if gp else None),
            'win':100*sum(x>0 for x in vals)/len(vals),'median':median(vals)}


def _peak_concurrency(rows):
    events=[]
    for cid,ts in rows:
        try:
            dt=datetime.fromisoformat(str(ts)); dt=dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except Exception: continue
        events.append((dt,1,int(cid))); events.append((dt+timedelta(minutes=HORIZON_MINUTES),-1,int(cid)))
    # closes before opens at identical timestamp
    events.sort(key=lambda x:(x[0],x[1]))
    n=peak=0; when=None
    for ts,delta,_ in events:
        n+=delta
        if n>peak: peak=n; when=ts
    return peak,when


def get_pilot_engine_audit_v1_report():
    with closing(get_connection()) as c:
        out={}
        for cp in CHECKPOINTS:
            for label,where in [('TRADE',"d.action='TRADE'"),('SKIP_MAX_OPEN',"d.action='SKIP' AND d.reason='MAX_OPEN_POSITIONS'")]:
                vals=[r[0] for r in c.execute(f'''SELECT o.roi FROM pilot_engine_v1_decisions d
                    JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
                    WHERE d.version=? AND {where} AND o.checkpoint_minutes=?
                    ORDER BY d.decided_at,d.candidate_id''',(VERSION,cp)).fetchall()]
                out[(label,cp)]=_stats(vals)
        trade_rows=c.execute("SELECT candidate_id,decided_at FROM pilot_engine_v1_decisions WHERE version=? AND action='TRADE' ORDER BY decided_at,candidate_id",(VERSION,)).fetchall()
        peak,peak_at=_peak_concurrency(trade_rows)
        now=datetime.now(timezone.utc); cutoff=(now-timedelta(minutes=HORIZON_MINUTES)).isoformat()
        active=int(c.execute("SELECT COUNT(*) FROM pilot_engine_v1_decisions WHERE version=? AND action='TRADE' AND decided_at>=? AND decided_at<=?",(VERSION,cutoff,now.isoformat())).fetchone()[0] or 0)
        stale=int(c.execute('''SELECT COUNT(*) FROM pilot_engine_v1_decisions d LEFT JOIN entry_discovery_outcomes o
          ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=?
          WHERE d.version=? AND d.action='TRADE' AND d.decided_at<? AND o.candidate_id IS NULL''',(HORIZON_MINUTES,VERSION,cutoff)).fetchone()[0] or 0)
        totals=dict(c.execute("SELECT action,COUNT(*) FROM pilot_engine_v1_decisions WHERE version=? GROUP BY action",(VERSION,)).fetchall())
        skip_total=int(c.execute("SELECT COUNT(*) FROM pilot_engine_v1_decisions WHERE version=? AND action='SKIP' AND reason='MAX_OPEN_POSITIONS'",(VERSION,)).fetchone()[0] or 0)
    return {'stats':out,'peak':peak,'peak_at':peak_at,'active':active,'stale_missing_24h':stale,'totals':totals,'skip_max':skip_total}


def _pct(v):return '—' if v is None else f'{v:+.1f}%'
def _pf(v):
    if v is None:return '—'
    return '∞' if math.isinf(v) else f'{v:.2f}'
def _line(label,s):return f"• {label}: n={s['n']} · ROI {_pct(s['roi'])} · PF {_pf(s['pf'])} · Win {_pct(s['win'])} · Med {_pct(s['median'])}"


def format_pilot_engine_audit_v1_report(r):
    lines=['🔬 Pilot Engine Audit v1 · READ-ONLY','TRADE vs MAX_OPEN counterfactual · same future outcomes','']
    for cp,name in [(180,'3ч'),(360,'6ч'),(720,'12ч'),(1440,'24ч')]:
        lines += [f'⏱ {name}',_line('TRADE',r['stats'][('TRADE',cp)]),_line('SKIP MAX_OPEN',r['stats'][('SKIP_MAX_OPEN',cp)]),'']
    pa='—' if r['peak_at'] is None else r['peak_at'].strftime('%d.%m %H:%M UTC')
    lines += ['🧮 Concurrency audit',f"• Peak 24ч concurrent TRADE: {r['peak']} (at {pa})",f"• Active by 24ч window now: {r['active']}",f"• >24ч без записанного 24ч outcome: {r['stale_missing_24h']}",f"• Decisions: TRADE {int(r['totals'].get('TRADE',0))} · MAX_OPEN SKIP {r['skip_max']}",'',
              'ℹ️ Аудит ничего не меняет в Pilot. SKIP — counterfactual: что произошло бы с пропущенными кандидатами на тех же горизонтах.']
    return '\n'.join(lines)
