"""Pilot v2 OOS Review #3 — frozen read-only cohort for TRADE decisions 31–45.

No rows are written and no Pilot settings, limits, exits, decisions, or orders change.
Adds robustness diagnostics so single extreme winners/losers can be seen explicitly.
"""
from __future__ import annotations
from contextlib import closing
from statistics import median
from database import get_connection
from pilot_engine_v2 import VERSION

HORIZONS=(180,360,720,1440)
LABELS={180:'3h',360:'6h',720:'12h',1440:'24h'}
START_INDEX=30
MILESTONE_N=15


def _stats(rois, stake=20.0):
    vals=[float(x) for x in rois if x is not None]
    if not vals:return {'n':0,'roi':None,'pf':None,'win':None,'median':None,'pnl':0.0}
    pnls=[stake*r/100.0 for r in vals]; gp=sum(max(x,0) for x in pnls); gl=-sum(min(x,0) for x in pnls)
    return {'n':len(vals),'roi':sum(vals)/len(vals),'pf':gp/gl if gl else (float('inf') if gp else None),
            'win':100.0*sum(r>0 for r in vals)/len(vals),'median':median(vals),'pnl':sum(pnls)}


def _bucket_stats(rows,key,buckets):
    out=[]
    for label,lo,hi in buckets:
        vals=[r['roi24'] for r in rows if r[key] is not None and float(r[key])>=lo and (hi is None or float(r[key])<hi)]
        x=_stats(vals); x['label']=label; out.append(x)
    return out


def _robustness(vals):
    vals=[float(x) for x in vals if x is not None]
    if not vals:return {'full':_stats([]),'without_best':_stats([]),'without_worst':_stats([]),'without_both':_stats([]),'best':None,'worst':None}
    best=max(vals); worst=min(vals)
    no_best=list(vals); no_best.remove(best)
    no_worst=list(vals); no_worst.remove(worst)
    no_both=list(vals); no_both.remove(best)
    if no_both:
        # If best == worst, remove one additional observation only when available.
        try:no_both.remove(worst)
        except ValueError:pass
    return {'full':_stats(vals),'without_best':_stats(no_best),'without_worst':_stats(no_worst),
            'without_both':_stats(no_both),'best':best,'worst':worst}


def get_pilot_oos_review_v2_report():
    with closing(get_connection()) as c:
        base=c.execute("""SELECT d.candidate_id,e.title,d.decided_at,d.entry_yes,d.early_score,d.acceleration,d.stake,o.roi
          FROM pilot_engine_v2_decisions d
          JOIN entry_discovery_candidates e ON e.id=d.candidate_id
          JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=1440
          WHERE d.version=? AND d.action='TRADE'
          ORDER BY d.decided_at,d.candidate_id""",(VERSION,)).fetchall()
        cohort=base[START_INDEX:START_INDEX+MILESTONE_N]
        review1=base[:MILESTONE_N]
        ids=[int(r[0]) for r in cohort]
        ids1=[int(r[0]) for r in review1]
        all_ids=ids1+ids
        outcome_map={cid:{} for cid in all_ids}
        if all_ids:
            ph=','.join('?' for _ in all_ids)
            for cid,h,roi in c.execute(f"SELECT candidate_id,checkpoint_minutes,roi FROM entry_discovery_outcomes WHERE candidate_id IN ({ph}) AND checkpoint_minutes IN (180,360,720,1440)",all_ids).fetchall():
                outcome_map[int(cid)][int(h)]=float(roi)
    rows=[]
    for cid,title,decided,price,early,accel,stake,roi24 in cohort:
        rows.append({'candidate_id':int(cid),'title':str(title),'decided_at':str(decided),'price':float(price),'early':float(early),'accel':float(accel),'stake':float(stake),'roi24':float(roi24),'outcomes':outcome_map.get(int(cid),{})})
    horizons={h:_stats([r['outcomes'][h] for r in rows if h in r['outcomes']]) for h in HORIZONS}
    robustness={h:_robustness([r['outcomes'][h] for r in rows if h in r['outcomes']]) for h in HORIZONS}
    comparison={}
    for h in HORIZONS:
        s1=_stats([outcome_map[int(r[0])][h] for r in review1 if h in outcome_map.get(int(r[0]),{})])
        s2=horizons[h]
        comparison[h]={'review1':s1,'review2':s2,'roi_delta':None if s1['roi'] is None or s2['roi'] is None else s2['roi']-s1['roi']}
    price_buckets=_bucket_stats(rows,'price',[('20–29¢',0.20,0.30),('30–39¢',0.30,0.40),('40–50¢',0.40,0.501)])
    early_buckets=_bucket_stats(rows,'early',[('60–64',60,65),('65–69',65,70)])
    accel_buckets=_bucket_stats(rows,'accel',[('0.68–0.79',0.68,0.80),('0.80–0.94',0.80,0.95),('0.95–1.07',0.95,1.071)])
    best={'3h':0,'6h':0,'12h':0,'24h':0}
    for r in rows:
        if all(h in r['outcomes'] for h in HORIZONS):
            bh=max(HORIZONS,key=lambda h:r['outcomes'][h]); best[LABELS[bh]]+=1
    ranked=sorted(rows,key=lambda r:r['roi24'],reverse=True)
    return {'mode':'READ_ONLY','review':'Pilot v2 OOS Review #3','milestone_n':MILESTONE_N,'cohort_n':len(rows),
      'available_complete_24h':len(base),'locked_definition':'TRADE decisions 31–45 with recorded 24h outcome',
      'horizons':horizons,'robustness':robustness,'comparison':comparison,
      'price_buckets':price_buckets,'early_buckets':early_buckets,'accel_buckets':accel_buckets,'best_horizon_counts':best,
      'top_24h':[{'title':r['title'],'roi24':r['roi24'],'price':r['price'],'early':r['early'],'accel':r['accel']} for r in ranked[:5]],
      'bottom_24h':[{'title':r['title'],'roi24':r['roi24'],'price':r['price'],'early':r['early'],'accel':r['accel']} for r in ranked[-5:]],
      'note':'Frozen descriptive review. Robustness removes observed extremes analytically only; no trade is deleted from canonical results.'}
