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

# Diagnostic buckets are intentionally read-only. They use the frozen entry
# features already stored with every Pilot decision and never affect eligibility.
FEATURE_BUCKETS = {
    "Price": [
        ("20–29¢", "d.entry_yes>=0.20 AND d.entry_yes<0.30"),
        ("30–39¢", "d.entry_yes>=0.30 AND d.entry_yes<0.40"),
        ("40–50¢", "d.entry_yes>=0.40 AND d.entry_yes<=0.50"),
    ],
    "Early": [
        ("60–64", "d.early_score>=60 AND d.early_score<65"),
        ("65–69", "d.early_score>=65 AND d.early_score<=69"),
    ],
    "Accel": [
        ("0.05–0.10", "d.acceleration>0.05 AND d.acceleration<0.10"),
        ("0.10–0.20", "d.acceleration>=0.10 AND d.acceleration<0.20"),
        ("≥0.20", "d.acceleration>=0.20"),
    ],
}


def _stats(vals):
    vals=[float(x) for x in vals]
    if not vals:return {'n':0,'roi':None,'pf':None,'win':None,'median':None}
    gp=sum(max(x,0) for x in vals); gl=-sum(min(x,0) for x in vals)
    return {'n':len(vals),'roi':sum(vals)/len(vals),'pf':gp/gl if gl else (float('inf') if gp else None),
            'win':100*sum(x>0 for x in vals)/len(vals),'median':median(vals)}


def _distribution(vals):
    vals=sorted(float(x) for x in vals if x is not None)
    if not vals:
        return {'n':0,'min':None,'median':None,'max':None,'q1':None,'q3':None}
    def q(frac):
        if len(vals)==1:return vals[0]
        pos=(len(vals)-1)*frac; lo=int(pos); hi=min(lo+1,len(vals)-1); w=pos-lo
        return vals[lo]*(1-w)+vals[hi]*w
    return {'n':len(vals),'min':vals[0],'median':median(vals),'max':vals[-1],'q1':q(.25),'q3':q(.75)}


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

        # 24h feature diagnostic across decisions that were otherwise eligible:
        # actual TRADE + candidates blocked only by MAX_OPEN. This avoids treating
        # the concurrency gate as a feature-quality signal.
        feature_stats={}
        eligible_where="(d.action='TRADE' OR (d.action='SKIP' AND d.reason='MAX_OPEN_POSITIONS'))"
        for feature,buckets in FEATURE_BUCKETS.items():
            feature_stats[feature]=[]
            for bucket,clause in buckets:
                vals=[r[0] for r in c.execute(f'''SELECT o.roi FROM pilot_engine_v1_decisions d
                    JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
                    WHERE d.version=? AND {eligible_where} AND o.checkpoint_minutes=? AND {clause}
                    ORDER BY d.decided_at,d.candidate_id''',(VERSION,HORIZON_MINUTES)).fetchall()]
                feature_stats[feature].append((bucket,_stats(vals)))

        # Raw acceleration scale: diagnostic only, so bucket boundaries can be
        # chosen from observed values instead of assumptions about units.
        accel_vals=[r[0] for r in c.execute(f'''SELECT d.acceleration FROM pilot_engine_v1_decisions d
            JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
            WHERE d.version=? AND {eligible_where} AND o.checkpoint_minutes=?
            ORDER BY d.acceleration''',(VERSION,HORIZON_MINUTES)).fetchall()]
        accel_distribution=_distribution(accel_vals)

        # Interaction diagnostic: price can behave differently at the two Early bands.
        interaction_stats=[]
        price_buckets=FEATURE_BUCKETS['Price']; early_buckets=FEATURE_BUCKETS['Early']
        for p_name,p_clause in price_buckets:
            for e_name,e_clause in early_buckets:
                vals=[r[0] for r in c.execute(f'''SELECT o.roi FROM pilot_engine_v1_decisions d
                    JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
                    WHERE d.version=? AND {eligible_where} AND o.checkpoint_minutes=?
                      AND {p_clause} AND {e_clause}
                    ORDER BY d.decided_at,d.candidate_id''',(VERSION,HORIZON_MINUTES)).fetchall()]
                interaction_stats.append((f'{p_name} × Early {e_name}',_stats(vals)))
    return {'stats':out,'peak':peak,'peak_at':peak_at,'active':active,'stale_missing_24h':stale,'totals':totals,'skip_max':skip_total,'feature_stats':feature_stats,'accel_distribution':accel_distribution,'interaction_stats':interaction_stats}


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
    lines += ['🧮 Concurrency audit',f"• Peak 24ч concurrent TRADE: {r['peak']} (at {pa})",f"• Active by 24ч window now: {r['active']}",f"• >24ч без записанного 24ч outcome: {r['stale_missing_24h']}",f"• Decisions: TRADE {int(r['totals'].get('TRADE',0))} · MAX_OPEN SKIP {r['skip_max']}",'']
    lines += ['🧬 24ч feature diagnostic · TRADE + MAX_OPEN']
    for feature in ('Price','Early','Accel'):
        lines.append(f'• {feature}:')
        for bucket,s in r.get('feature_stats',{}).get(feature,[]):
            lines.append(f"  {bucket}: n={s['n']} · ROI {_pct(s['roi'])} · PF {_pf(s['pf'])} · Win {_pct(s['win'])}")
    ad=r.get('accel_distribution',{})
    def _num(v): return '—' if v is None else f'{v:.4f}'
    lines += ['', '📐 Acceleration scale · 24ч sample',
              f"• n={ad.get('n',0)} · min {_num(ad.get('min'))} · Q1 {_num(ad.get('q1'))} · median {_num(ad.get('median'))} · Q3 {_num(ad.get('q3'))} · max {_num(ad.get('max'))}",
              '', '🧩 Price × Early · 24ч']
    for label,s in r.get('interaction_stats',[]):
        lines.append(f"• {label}: n={s['n']} · ROI {_pct(s['roi'])} · PF {_pf(s['pf'])} · Win {_pct(s['win'])}")
    lines += ['', 'ℹ️ Аудит ничего не меняет в Pilot. Feature diagnostic тоже READ-ONLY; SKIP — counterfactual: что произошло бы с пропущенными кандидатами на тех же горизонтах.']
    return '\n'.join(lines)
