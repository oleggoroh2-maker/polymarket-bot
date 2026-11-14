"""Read-only first 30 matured v4 OOS review against contemporaneous v2 PAPER trades."""
from contextlib import closing
from statistics import median
from database import get_connection
from pilot_engine_v4 import VERSION as V4_VERSION
from pilot_engine_v2 import VERSION as V2_VERSION
from pilot_market_regime_audit_v2 import category

HS=(180,360,720,1440)

def _stats(values):
    vals=[(float(stake),float(roi)) for stake,roi in values if roi is not None]
    if not vals:return dict(n=0,roi=None,pf=None,win=None,median=None,pnl=0.0)
    pnl=[s*r/100 for s,r in vals]; gp=sum(max(v,0) for v in pnl); gl=-sum(min(v,0) for v in pnl)
    return dict(n=len(vals),roi=sum(r for _,r in vals)/len(vals),pf=gp/gl if gl else ('Infinity' if gp else None),win=100*sum(r>0 for _,r in vals)/len(vals),median=median(r for _,r in vals),pnl=sum(pnl))

def _robust(rows):
    rows=sorted(rows,key=lambda x:x[1]); return dict(normal=_stats(rows),without_best=_stats(rows[:-1]),without_worst=_stats(rows[1:]),without_both=_stats(rows[1:-1]),best=rows[-1][1] if rows else None,worst=rows[0][1] if rows else None)

def report():
    with closing(get_connection()) as c:
        # Freeze membership by earliest 30 v4 TRADEs that have a recorded 24h checkpoint.
        base=c.execute('''SELECT d.candidate_id,d.decided_at,d.stake,d.entry_yes,d.acceleration,e.title,e.category
          FROM pilot_engine_v4_decisions d JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=1440
          JOIN entry_discovery_candidates e ON e.id=d.candidate_id
          WHERE d.version=? AND d.action='TRADE' ORDER BY d.decided_at,d.candidate_id LIMIT 30''',(V4_VERSION,)).fetchall()
        ids=[int(x[0]) for x in base]
        if not ids:return dict(mode='READ_ONLY',target=30,v4_selected=0,v2_selected=0,ready=False,note='Waiting for matured v4 PAPER decisions')
        start=min(str(x[1]) for x in base); end=max(str(x[1]) for x in base)
        v2=c.execute('''SELECT d.candidate_id,d.decided_at,d.stake,d.entry_yes,d.acceleration,e.title,e.category
          FROM pilot_engine_v2_decisions d JOIN entry_discovery_candidates e ON e.id=d.candidate_id
          WHERE d.version=? AND d.action='TRADE' AND d.decided_at>=? AND d.decided_at<=?
          ORDER BY d.decided_at,d.candidate_id''',(V2_VERSION,start,end)).fetchall()
        allids=sorted(set(ids+[int(x[0]) for x in v2])); outcomes={cid:{} for cid in allids}
        if allids:
            ph=','.join('?' for _ in allids)
            for cid,h,roi in c.execute(f'''SELECT candidate_id,checkpoint_minutes,roi FROM entry_discovery_outcomes
               WHERE candidate_id IN ({ph}) AND checkpoint_minutes IN (180,360,720,1440)''',allids).fetchall():
                if roi is not None:outcomes[int(cid)][int(h)]=float(roi)
    def rows_at(records,h):
        return [(float(r[2]),outcomes[int(r[0])][h]) for r in records if h in outcomes[int(r[0])]]
    horizons={str(h):dict(v4=_stats(rows_at(base,h)),v2=_stats(rows_at(v2,h))) for h in HS}
    complete=[r for r in base if all(h in outcomes[int(r[0])] for h in HS)]
    transitions=[]
    for a,b in zip(HS,HS[1:]):
        ds=[outcomes[int(r[0])][b]-outcomes[int(r[0])][a] for r in complete]
        transitions.append(dict(period=f'{a//60}h → {b//60}h',n=len(ds),improved=sum(d>0 for d in ds),declined=sum(d<0 for d in ds),unchanged=sum(d==0 for d in ds),avg_delta=sum(ds)/len(ds) if ds else None))
    categories={}
    for r in base:
        if 1440 in outcomes[int(r[0])]:
            cat=category(r[5],r[6]);categories.setdefault(cat,[]).append((float(r[2]),outcomes[int(r[0])][1440]))
    v4set=set(ids);v2set={int(r[0]) for r in v2}
    return dict(mode='READ_ONLY',target=30,v4_selected=len(base),v2_selected=len(v2),ready=len(base)==30,
      window_start=start,window_end=end,shared_candidate_ids=len(v4set&v2set),
      definition='First 30 v4 TRADE decisions with recorded 24h checkpoint; v2 TRADE decisions within the same UTC decision-time window. Independent cohorts, not paired trades.',
      horizons=horizons,robustness={str(h):_robust(rows_at(base,h)) for h in HS},
      categories=[dict(label=k,**_stats(v)) for k,v in sorted(categories.items())],transitions=transitions,
      note='Descriptive PAPER outcomes only; selection bias, different trade counts, market conditions and position limits can affect comparison. No executable exits, fees, spread, slippage or liquidity model. Trading logic unchanged.')
