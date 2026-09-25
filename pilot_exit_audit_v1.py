"""Read-only Pilot v2 Position / Exit Audit.

Analyses already-recorded PAPER outcomes at 3h/6h/12h/24h. It never creates,
changes, closes, or deletes Pilot decisions/orders.
"""
from __future__ import annotations
from contextlib import closing
from collections import Counter
from statistics import median
import math
import config
from database import get_connection
from pilot_engine_v2 import VERSION, HORIZONS

LABELS = {180:'3h', 360:'6h', 720:'12h', 1440:'24h'}

def _stake(): return float(getattr(config, 'PILOT_V2_STAKE_USD', 20.0))

def _stats(rois):
    vals=[float(x) for x in rois if x is not None]
    if not vals:return {'n':0,'roi':None,'pf':None,'win':None,'median':None,'pnl':0.0}
    pnls=[_stake()*r/100.0 for r in vals]; gp=sum(max(x,0) for x in pnls); gl=-sum(min(x,0) for x in pnls)
    return {'n':len(vals),'roi':sum(vals)/len(vals),'pf':gp/gl if gl else (float('inf') if gp else None),
            'win':100.0*sum(r>0 for r in vals)/len(vals),'median':median(vals),'pnl':sum(pnls)}

def get_pilot_exit_audit_v1_report():
    with closing(get_connection()) as c:
        rows=c.execute("""SELECT d.candidate_id,e.title,d.decided_at,o.checkpoint_minutes,o.roi
            FROM pilot_engine_v2_decisions d
            JOIN entry_discovery_candidates e ON e.id=d.candidate_id
            JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
            WHERE d.version=? AND d.action='TRADE' AND o.checkpoint_minutes IN (180,360,720,1440)
            ORDER BY d.decided_at,d.candidate_id,o.checkpoint_minutes""",(VERSION,)).fetchall()
    trades={}
    for cid,title,decided,h,roi in rows:
        x=trades.setdefault(int(cid),{'title':str(title),'decided_at':str(decided),'outcomes':{}})
        x['outcomes'][int(h)]=float(roi)
    horizons={h:_stats([t['outcomes'][h] for t in trades.values() if h in t['outcomes']]) for h in HORIZONS}
    transitions={}
    for a,b in zip(HORIZONS,HORIZONS[1:]):
        pairs=[(t['outcomes'][a],t['outcomes'][b]) for t in trades.values() if a in t['outcomes'] and b in t['outcomes']]
        diffs=[y-x for x,y in pairs]
        transitions[f'{LABELS[a]}→{LABELS[b]}']={
            'n':len(pairs),'improved':sum(y>x for x,y in pairs),'declined':sum(y<x for x,y in pairs),
            'unchanged':sum(y==x for x,y in pairs),'avg_delta_roi':sum(diffs)/len(diffs) if diffs else None,
            'median_delta_roi':median(diffs) if diffs else None,
        }
    best=Counter(); complete=[]
    for cid,t in trades.items():
        if all(h in t['outcomes'] for h in HORIZONS):
            bh=max(HORIZONS,key=lambda h:t['outcomes'][h]); best[LABELS[bh]]+=1
            complete.append({'candidate_id':cid,'title':t['title'],'decided_at':t['decided_at'],
                             'best_horizon':LABELS[bh],'best_roi':t['outcomes'][bh],
                             'rois':{LABELS[h]:t['outcomes'][h] for h in HORIZONS}})
    complete.sort(key=lambda x:x['decided_at'],reverse=True)
    return {'mode':'READ_ONLY','experiment':'pilot_v2_position_exit_audit_v1','trade_total':len(trades),
            'complete_24h_trades':len(complete),'horizons':horizons,'transitions':transitions,
            'best_horizon_counts':dict(best),'recent_complete':complete[:10],
            'note':'Descriptive audit of recorded PAPER checkpoints only; no exit rule or Pilot decision is changed.'}
