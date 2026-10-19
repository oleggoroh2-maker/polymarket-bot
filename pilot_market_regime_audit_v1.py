"""Read-only Pilot v2 context audit. Does not infer historical regime labels."""
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from statistics import median
from database import get_connection
from pilot_engine_v2 import VERSION

def _stats(rows):
    a=[(float(stake),float(roi)) for stake,roi in rows]
    if not a:return dict(n=0,roi=None,pf=None,win=None,median=None,pnl=0.0)
    p=[s*r/100 for s,r in a]; gp=sum(max(0,x) for x in p); gl=-sum(min(0,x) for x in p)
    return dict(n=len(a),roi=sum(r for _,r in a)/len(a),pf=(gp/gl if gl else ('Infinity' if gp else None)),win=100*sum(r>0 for _,r in a)/len(a),median=median(r for _,r in a),pnl=sum(p))

def get_pilot_market_regime_audit_v1_report():
    with closing(get_connection()) as c:
        rows=c.execute('''SELECT d.candidate_id,d.decided_at,d.stake,e.category,e.change_1h,e.change_24h,
          o.roi FROM pilot_engine_v2_decisions d JOIN entry_discovery_candidates e ON e.id=d.candidate_id
          LEFT JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=1440
          WHERE d.version=? AND d.action='TRADE' ORDER BY d.decided_at,d.candidate_id''',(VERSION,)).fetchall()
    groups={k:defaultdict(list) for k in ('category','period','move_1h','move_24h')}
    missing=0; complete=0
    for cid,t,stake,cat,c1,c24,roi in rows:
        if roi is None:continue
        complete+=1
        try:day=datetime.fromisoformat(str(t).replace('Z','+00:00')).astimezone(timezone.utc).strftime('%Y-%m-%d')
        except (ValueError,TypeError):day='Unknown'
        values={'category':str(cat or 'Unknown'), 'period':day}
        for key,v in [('move_1h',c1),('move_24h',c24)]:
            if v is None:values[key]='Unknown';missing+=1
            else:
                a=abs(float(v));values[key]=('<5%' if a<5 else '5–15%' if a<15 else '15–30%' if a<30 else '≥30%')
        for key,label in values.items():groups[key][label].append((stake,roi))
    return dict(mode='READ_ONLY',total_trades=len(rows),matured=complete,
      sections={k:[dict(label=label,**_stats(v)) for label,v in sorted(bucket.items(),key=lambda kv:(-len(kv[1]),kv[0]))] for k,bucket in groups.items()},
      regime_status='UNAVAILABLE_FOR_PILOT_HISTORY',
      note='Existing Market Regime Engine classifies alert events, not Pilot v2 entry decisions. No historical regime label is joined or reconstructed here. Absolute candidate change_1h/change_24h are descriptive entry-move proxies, NOT market-wide regime or manipulation proof. PAPER checkpoint outcomes do not model executable exits, spread, slippage, or fees. No decisions or database rows changed.')
