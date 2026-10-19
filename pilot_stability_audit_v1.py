"""Read-only, fixed first 100 matured Pilot v2 trades stability audit."""
from contextlib import closing
from statistics import median
from database import get_connection
from pilot_engine_v2 import VERSION

HS=(180,360,720,1440)
def stats(values):
    a=[float(x) for x in values if x is not None]
    if not a:return dict(n=0,roi=None,pf=None,win=None,median=None,pnl=0.0)
    gains=sum(max(x,0) for x in a); losses=-sum(min(x,0) for x in a)
    return dict(n=len(a),roi=sum(a)/len(a),pf=(gains/losses if losses else (None if not gains else 'Infinity')),win=100*sum(x>0 for x in a)/len(a),median=median(a),pnl=sum(a)*.2)
def get_pilot_stability_audit_v1_report():
    with closing(get_connection()) as c:
        base=c.execute("""SELECT d.candidate_id,d.decided_at,d.entry_yes,d.acceleration,d.early_score,o.roi
          FROM pilot_engine_v2_decisions d
          JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=1440
          WHERE d.version=? AND d.action='TRADE'
          ORDER BY d.decided_at,d.candidate_id LIMIT 100""",(VERSION,)).fetchall()
        ids=[int(r[0]) for r in base]; outcomes={cid:{} for cid in ids}
        if ids:
            ph=','.join('?' for _ in ids)
            for cid,h,roi in c.execute(f"SELECT candidate_id,checkpoint_minutes,roi FROM entry_discovery_outcomes WHERE candidate_id IN ({ph}) AND checkpoint_minutes IN (180,360,720,1440)",ids).fetchall():
                outcomes[int(cid)][int(h)]=float(roi)
    trades=[dict(id=int(cid),time=str(t),price=float(price),accel=float(accel),early=float(early),outcomes=outcomes[int(cid)]) for cid,t,price,accel,early,_ in base]
    complete=[r for r in trades if all(h in r['outcomes'] for h in HS)]
    horizons={str(h):stats([r['outcomes'][h] for r in complete]) for h in HS}
    quarters=[dict(label=f'Trades {i+1}–{min(i+25,100)}',horizons={str(h):stats([r['outcomes'][h] for r in trades[i:i+25] if h in r['outcomes']]) for h in HS}) for i in range(0,100,25)]
    robustness={}
    for h in HS:
        vals=[r['outcomes'][h] for r in complete]; ordered=sorted(vals)
        robustness[str(h)]={'normal':stats(vals),'without_best':stats(ordered[:-1]),'without_worst':stats(ordered[1:]),'without_both':stats(ordered[1:-1]),'best':max(vals) if vals else None,'worst':min(vals) if vals else None}
    def bucket(key,defs):
        return [dict(label=label,**stats([r['outcomes'][1440] for r in complete if lo<=r[key]<hi])) for label,lo,hi in defs]
    price=bucket('price',[('20–29¢',.20,.30),('30–39¢',.30,.40),('40–50¢',.40,.501)])
    accel=bucket('accel',[('0.68–0.79',.68,.80),('0.80–0.94',.80,.95),('0.95–1.07',.95,1.071)])
    pairs={}
    for a,b in zip(HS,HS[1:]):
        diffs=[r['outcomes'][b]-r['outcomes'][a] for r in complete]
        pairs[f'{a}→{b}']=dict(n=len(diffs),improved=sum(x>0 for x in diffs),declined=sum(x<0 for x in diffs),unchanged=sum(x==0 for x in diffs),avg_delta=sum(diffs)/len(diffs) if diffs else None)
    return dict(mode='READ_ONLY',cohort_n=len(trades),complete_n=len(complete),target_n=100,
        definition='First 100 Pilot v2 TRADE decisions with recorded 24h outcome, ordered by decision time; paired horizons use only fully complete trades',
        horizons=horizons,quarters=quarters,robustness=robustness,price_buckets=price,accel_buckets=accel,transitions=pairs,
        note='Descriptive PAPER checkpoint outcomes, not executable exit prices. No fees, spread, slippage or liquidity model. No trading rules changed.')
