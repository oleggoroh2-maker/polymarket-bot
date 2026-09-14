"""Candidate Validation v2 — robustness audit for Stable Zone Shadow v1.

Read-only Shadow/Paper analytics. The stable-zone definition is never changed.
Uses the already frozen Stable Zone cohort and its future outcomes.
"""
from __future__ import annotations
from contextlib import closing
import json, math
from statistics import median
from database import get_connection
from stable_zone_shadow import VERSION as ZONE_VERSION

CP = 1440


def _stats(vals):
    vals=[float(x) for x in vals]; n=len(vals)
    if not n:return {'n':0,'roi':None,'pf':None,'win':None,'median':None,'pnl':0.0}
    gp=sum(max(x,0) for x in vals); gl=-sum(min(x,0) for x in vals)
    return {'n':n,'roi':sum(vals)/n,'pf':gp/gl if gl else (float('inf') if gp else None),
            'win':100*sum(x>0 for x in vals)/n,'median':median(vals),'pnl':sum(vals)}

def _bucket(v, cuts, labels):
    if v is None:return 'UNKNOWN'
    x=float(v)
    for c,l in zip(cuts,labels):
        if x<c:return l
    return labels[-1]

def _fmt(s):
    roi='—' if s['roi'] is None else f"{s['roi']:+.1f}%"
    pf='—' if s['pf'] is None else ('∞' if math.isinf(s['pf']) else f"{s['pf']:.2f}")
    return f"n={s['n']} · ROI {roi} · PF {pf}"

def get_candidate_validation_v2_report():
    with closing(get_connection()) as c:
        rows=c.execute('''SELECT z.candidate_id,z.side,z.entry_yes,z.early_score,o.roi,z.frozen_at,
          f.category_v2,f.spread,f.acceleration,f.bid_balance
          FROM stable_zone_shadow_frozen z
          JOIN entry_discovery_outcomes o ON o.candidate_id=z.candidate_id AND o.checkpoint_minutes=?
          LEFT JOIN entry_feature_snapshots f ON f.candidate_id=z.candidate_id
          WHERE z.version=? ORDER BY z.frozen_at,z.candidate_id''',(CP,ZONE_VERSION)).fetchall()
    vals=[float(r[4]) for r in rows]
    overall=_stats(vals)
    rolling={w:_stats(vals[-w:]) for w in (20,50,100) if vals}
    segs={}
    def add(name,key,val):segs.setdefault(name,{}).setdefault(key,[]).append(val)
    for cid,side,p,early,roi,ts,cat,spread,acc,bal in rows:
        v=float(roi)
        add('Side',str(side),v)
        add('Category',str(cat or 'UNKNOWN'),v)
        add('Price',_bucket(p,[.30,.40,float('inf')],['20–30¢','30–40¢','40–50¢']),v)
        add('Early',_bucket(early,[65,float('inf')],['60–64','65–69']),v)
        if spread is not None:add('Spread',_bucket(spread,[.02,.05,float('inf')],['<2¢','2–5¢','5¢+']),v)
        if acc is not None:add('Acceleration','UP' if float(acc)>0.05 else 'FLAT/DOWN',v)
        if bal is not None:add('BidBalance','40%+' if float(bal)>=40 else '<40%',v)
    segments={name:{k:_stats(v) for k,v in d.items()} for name,d in segs.items()}
    wins=sorted([x for x in vals if x>0],reverse=True); gp=sum(wins)
    contrib={k:(100*sum(wins[:k])/gp if gp>0 else None) for k in (1,5,10)}
    r50=rolling.get(50,overall)
    top5=contrib[5]
    gate={
      'n150':overall['n']>=150,
      'roi':overall['roi'] is not None and overall['roi']>.75,
      'pf':overall['pf'] is not None and overall['pf']>1.25,
      'rolling50':r50['roi'] is not None and r50['roi']>0,
      'concentration':top5 is not None and top5<60,
    }
    status='🟢 ROBUST CANDIDATE' if all(gate.values()) else ('🔴 FAILED ROBUSTNESS' if overall['n']>=150 else '🟡 VALIDATING')
    return {'overall':overall,'rolling':rolling,'segments':segments,'contrib':contrib,'gate':gate,'status':status}

def format_candidate_validation_v2_report(r):
    o=r["overall"]
    win="—" if o["win"] is None else f'{o["win"]:.1f}%'
    med="—" if o["median"] is None else f'{o["median"]:+.1f}%'
    lines=["🛡 Candidate Validation v2 · FUTURE-ONLY","Zone locked: Price 20–50¢ × Early 60–69","Primary horizon: 24ч",f'Sтатус: {r["status"]}',"",f"📊 All frozen: {_fmt(o)} · Win {win} · Median {med}","","📡 Rolling"]
    for w in (20,50,100):
        if w in r["rolling"]: lines.append(f"• Last {w}: {_fmt(r['rolling'][w])}")
    lines += ["","🧩 Robustness slices"]
    for name in ("Side","Category","Price","Early","Spread","Acceleration","BidBalance"):
        d=r["segments"].get(name,{})
        if d: lines.append("• "+name+": "+" · ".join(f"{k} {_fmt(v)}" for k,v in sorted(d.items())))
    lines += ["","🏆 Gross-profit concentration"]
    for k in (1,5,10):
        v=r["contrib"][k]; shown="—" if v is None else f"{v:.1f}%"
        lines.append(f"• Top-{k}: {shown} of gross profit")
    g=r["gate"]; roi="—" if o["roi"] is None else f'{o["roi"]:+.1f}%'; pf="—" if o["pf"] is None else f'{o["pf"]:.2f}'
    r50=r["rolling"].get(50,{}).get("roi"); r50s="—" if r50 is None else f"{r50:+.1f}%"
    c5=r["contrib"][5]; c5s="—" if c5 is None else f"{c5:.1f}%"
    lines += ["","🧪 Pre-registered robustness gate",f"• 24ч n≥150: {'✅' if g['n150'] else '⏳'} ({o['n']})",f"• Cumulative ROI > +0.75%: {'✅' if g['roi'] else '⏳'} ({roi})",f"• PF > 1.25: {'✅' if g['pf'] else '⏳'} ({pf})",f"• Rolling-50 ROI > 0: {'✅' if g['rolling50'] else '⏳'} ({r50s})",f"• Top-5 <60% gross profit: {'✅' if g['concentration'] else '⏳'} ({c5s})","","ℹ️ Read-only Shadow/Paper audit. Stable Zone membership and all Live/Trade logic remain unchanged."]
    return "\n".join(lines)
