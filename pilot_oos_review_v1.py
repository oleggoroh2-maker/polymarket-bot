"""Pilot v2 OOS Review #1 — read-only milestone review.

Freezes the analytical cohort logically at the first 15 Pilot v2 TRADE decisions
that have a recorded 24h outcome. No rows are written and no Pilot settings change.
"""
from __future__ import annotations
from contextlib import closing
from statistics import median
import math
from database import get_connection
from pilot_engine_v2 import VERSION

HORIZONS=(180,360,720,1440)
LABELS={180:'3h',360:'6h',720:'12h',1440:'24h'}
MILESTONE_N=15


def _stats(rois, stake=20.0):
    vals=[float(x) for x in rois if x is not None]
    if not vals:return {'n':0,'roi':None,'pf':None,'win':None,'median':None,'pnl':0.0}
    pnls=[stake*r/100.0 for r in vals]; gp=sum(max(x,0) for x in pnls); gl=-sum(min(x,0) for x in pnls)
    return {'n':len(vals),'roi':sum(vals)/len(vals),'pf':gp/gl if gl else (float('inf') if gp else None),
            'win':100.0*sum(r>0 for r in vals)/len(vals),'median':median(vals),'pnl':sum(pnls)}


def _bucket_stats(rows, key, buckets):
    out=[]
    for label,lo,hi in buckets:
        vals=[r['roi24'] for r in rows if r[key] is not None and float(r[key])>=lo and (hi is None or float(r[key])<hi)]
        x=_stats(vals); x['label']=label; out.append(x)
    return out


def get_pilot_oos_review_v1_report():
    with closing(get_connection()) as c:
        base=c.execute("""SELECT d.candidate_id,e.title,d.decided_at,d.entry_yes,d.early_score,d.acceleration,d.stake,o.roi
          FROM pilot_engine_v2_decisions d
          JOIN entry_discovery_candidates e ON e.id=d.candidate_id
          JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=1440
          WHERE d.version=? AND d.action='TRADE'
          ORDER BY d.decided_at,d.candidate_id""",(VERSION,)).fetchall()
        cohort=base[:MILESTONE_N]
        ids=[int(r[0]) for r in cohort]
        outcome_map={cid:{} for cid in ids}
        if ids:
            ph=','.join('?' for _ in ids)
            for cid,h,roi in c.execute(f"SELECT candidate_id,checkpoint_minutes,roi FROM entry_discovery_outcomes WHERE candidate_id IN ({ph}) AND checkpoint_minutes IN (180,360,720,1440)",ids).fetchall():
                outcome_map[int(cid)][int(h)]=float(roi)
        skip_rows=c.execute("""SELECT d.candidate_id,d.entry_yes,d.early_score,d.acceleration,o.roi
          FROM pilot_engine_v2_decisions d JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=1440
          WHERE d.version=? AND d.action='SKIP' AND d.reason='MAX_OPEN_POSITIONS' ORDER BY d.decided_at,d.candidate_id""",(VERSION,)).fetchall()
    rows=[]
    for cid,title,decided,price,early,accel,stake,roi24 in cohort:
        rows.append({'candidate_id':int(cid),'title':str(title),'decided_at':str(decided),'price':float(price),'early':float(early),'accel':float(accel),'stake':float(stake),'roi24':float(roi24),'outcomes':outcome_map.get(int(cid),{})})
    horizons={}
    for h in HORIZONS:
        horizons[h]=_stats([r['outcomes'][h] for r in rows if h in r['outcomes']])
    price_buckets=_bucket_stats(rows,'price',[('20–29¢',0.20,0.30),('30–39¢',0.30,0.40),('40–50¢',0.40,0.501)])
    early_buckets=_bucket_stats(rows,'early',[('60–64',60,65),('65–69',65,70)])
    accel_buckets=_bucket_stats(rows,'accel',[('0.68–0.79',0.68,0.80),('0.80–0.94',0.80,0.95),('0.95–1.07',0.95,1.071)])
    best={'3h':0,'6h':0,'12h':0,'24h':0}
    for r in rows:
        if all(h in r['outcomes'] for h in HORIZONS):
            bh=max(HORIZONS,key=lambda h:r['outcomes'][h]); best[LABELS[bh]]+=1
    winners=sorted(rows,key=lambda r:r['roi24'],reverse=True)
    skip24=_stats([float(r[4]) for r in skip_rows])
    return {
      'mode':'READ_ONLY','review':'Pilot v2 OOS Review #1','milestone_n':MILESTONE_N,
      'cohort_n':len(rows),'available_complete_24h':len(base),'locked_definition':'first 15 TRADE decisions with recorded 24h outcome',
      'horizons':horizons,'price_buckets':price_buckets,'early_buckets':early_buckets,'accel_buckets':accel_buckets,
      'best_horizon_counts':best,'max_open_skip_24h':skip24,
      'top_24h':[{'title':r['title'],'roi24':r['roi24'],'price':r['price'],'early':r['early'],'accel':r['accel']} for r in winners[:5]],
      'bottom_24h':[{'title':r['title'],'roi24':r['roi24'],'price':r['price'],'early':r['early'],'accel':r['accel']} for r in winners[-5:]],
      'note':'Descriptive milestone review only. It does not alter Pilot v2, limits, exits, decisions, or orders.'
    }
