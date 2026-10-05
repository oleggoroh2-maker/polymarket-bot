"""Pilot v2 OOS Summary 1–45 — frozen read-only aggregate of Reviews #1–#3."""
from __future__ import annotations
from contextlib import closing
from statistics import median
from database import get_connection
from pilot_engine_v2 import VERSION

HORIZONS=(180,360,720,1440)
LABELS={180:'3h',360:'6h',720:'12h',1440:'24h'}
N=45

def _stats(vals, stake=20.0):
    vals=[float(x) for x in vals if x is not None]
    if not vals:return {'n':0,'roi':None,'pf':None,'win':None,'median':None,'pnl':0.0}
    pnls=[stake*r/100 for r in vals]; gp=sum(max(x,0) for x in pnls); gl=-sum(min(x,0) for x in pnls)
    return {'n':len(vals),'roi':sum(vals)/len(vals),'pf':gp/gl if gl else (float('inf') if gp else None),
            'win':100*sum(r>0 for r in vals)/len(vals),'median':median(vals),'pnl':sum(pnls)}

def _bucket(rows,key,buckets):
    out=[]
    for label,lo,hi in buckets:
        vals=[r['roi24'] for r in rows if r[key] is not None and float(r[key])>=lo and (hi is None or float(r[key])<hi)]
        x=_stats(vals); x['label']=label; out.append(x)
    return out

def get_pilot_oos_summary_v1_report():
    with closing(get_connection()) as c:
        base=c.execute("""SELECT d.candidate_id,e.title,d.decided_at,d.entry_yes,d.early_score,d.acceleration,d.stake,o.roi
          FROM pilot_engine_v2_decisions d
          JOIN entry_discovery_candidates e ON e.id=d.candidate_id
          JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=1440
          WHERE d.version=? AND d.action='TRADE'
          ORDER BY d.decided_at,d.candidate_id""",(VERSION,)).fetchall()
        cohort=base[:N]
        ids=[int(r[0]) for r in cohort]
        om={cid:{} for cid in ids}
        if ids:
            ph=','.join('?' for _ in ids)
            for cid,h,roi in c.execute(f"SELECT candidate_id,checkpoint_minutes,roi FROM entry_discovery_outcomes WHERE candidate_id IN ({ph}) AND checkpoint_minutes IN (180,360,720,1440)",ids).fetchall():
                om[int(cid)][int(h)]=float(roi)
    rows=[]
    for cid,title,decided,price,early,accel,stake,roi24 in cohort:
        rows.append({'id':int(cid),'title':str(title),'price':float(price),'early':float(early),'accel':float(accel),'roi24':float(roi24),'outcomes':om.get(int(cid),{})})
    horizons={h:_stats([r['outcomes'][h] for r in rows if h in r['outcomes']]) for h in HORIZONS}
    cohorts=[]
    for i in range(3):
        part=rows[i*15:(i+1)*15]
        cohorts.append({'label':f'Review #{i+1}','start':i*15+1,'end':(i+1)*15,
                        'horizons':{h:_stats([r['outcomes'][h] for r in part if h in r['outcomes']]) for h in HORIZONS}})
    price=_bucket(rows,'price',[('20–29¢',.20,.30),('30–39¢',.30,.40),('40–50¢',.40,.501)])
    accel=_bucket(rows,'accel',[('0.68–0.79',.68,.80),('0.80–0.94',.80,.95),('0.95–1.07',.95,1.071)])
    best={'3h':0,'6h':0,'12h':0,'24h':0}
    complete=0
    for r in rows:
        if all(h in r['outcomes'] for h in HORIZONS):
            complete+=1; bh=max(HORIZONS,key=lambda h:r['outcomes'][h]); best[LABELS[bh]]+=1
    return {'mode':'READ_ONLY','review':'Pilot v2 OOS Summary 1–45','cohort_n':len(rows),'target_n':N,
            'available_complete_24h':len(base),'complete_all_horizons':complete,
            'locked_definition':'first 45 TRADE decisions with recorded 24h outcome',
            'horizons':horizons,'cohorts':cohorts,'price_buckets':price,'accel_buckets':accel,
            'best_horizon_counts':best,
            'note':'Frozen descriptive aggregate of OOS Reviews #1–#3. No Pilot rule, decision, limit, exit, or order is changed.'}
