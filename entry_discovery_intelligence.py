"""Entry Discovery Intelligence v2 — shadow analytics and future-only challenger."""
from __future__ import annotations
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json, math
from database import get_connection
from category_intelligence import classify_category

VERSION='v2-entry-intelligence'
CHECKPOINTS=(60,180,360,720,1440)

def ensure_schema():
    with closing(get_connection()) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS entry_discovery_intel_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entry_discovery_v2_frozen(
          candidate_id INTEGER PRIMARY KEY, frozen_at TEXT NOT NULL, v2_score REAL NOT NULL,
          tier TEXT NOT NULL, eligible INTEGER NOT NULL, version TEXT NOT NULL);
        ''')
        c.commit()

def ensure_launch():
    ensure_schema()
    with closing(get_connection()) as c:
        row=c.execute("SELECT value FROM entry_discovery_intel_meta WHERE key='launch_at'").fetchone()
        if row:return row[0]
        now=datetime.now(timezone.utc).isoformat()
        c.execute("INSERT INTO entry_discovery_intel_meta(key,value) VALUES('launch_at',?)",(now,));c.commit();return now

def _score(row):
    # Fixed pre-registered challenger. It is intentionally not fitted on future outcomes.
    title,side,price,early,reasons,vol,liq=row
    try: rs=set(json.loads(reasons or '[]'))
    except: rs=set()
    s=35.0 + max(0,min(25,(float(early or 55)-55)*0.8))
    if 'VOLUME_BUILD' in rs:s+=12
    if 'LIQUIDITY_BUILD' in rs:s+=8
    if 'PRE_BREAKOUT_MOVE' in rs:s+=8
    if 'PRICE_ALIGNMENT' in rs:s+=5
    p=float(price or 0)
    if .05<=p<.50:s+=5
    if float(vol or 0)>=80:s+=5
    if float(liq or 0)>=30:s+=3
    return max(0,min(100,s))

def freeze_new_candidates():
    launch=ensure_launch(); now=datetime.now(timezone.utc).isoformat(); added=0
    with closing(get_connection()) as c:
        rows=c.execute('''SELECT e.id,e.title,e.side,e.entry_yes,e.early_score,e.reasons_json,e.volume_change,e.liquidity_change
          FROM entry_discovery_candidates e LEFT JOIN entry_discovery_v2_frozen f ON f.candidate_id=e.id
          WHERE f.candidate_id IS NULL AND e.opened_at>=? ORDER BY e.id''',(launch,)).fetchall()
        for cid,*data in rows:
            sc=_score(tuple(data)); tier='80+' if sc>=80 else ('70-79' if sc>=70 else ('60-69' if sc>=60 else '<60'))
            c.execute('INSERT OR IGNORE INTO entry_discovery_v2_frozen VALUES(?,?,?,?,?,?)',(cid,now,sc,tier,1 if sc>=70 else 0,VERSION));added+=1
        c.commit()
    return added

def _stats(vals):
    n=len(vals)
    if not n:return {'n':0,'roi':None,'pf':None}
    gp=sum(max(v,0) for v in vals);gl=-sum(min(v,0) for v in vals)
    return {'n':n,'roi':sum(vals)/n,'pf':gp/gl if gl else (float('inf') if gp else None)}

def _fmt(s):
    roi='—' if s['roi'] is None else f"{s['roi']:+.1f}%"; pf='—' if s['pf'] is None else ('∞' if math.isinf(s['pf']) else f"{s['pf']:.2f}")
    return f"n={s['n']} ROI {roi} PF {pf}"

def get_entry_intelligence_report(cp=360):
    freeze_new_candidates()
    with closing(get_connection()) as c:
        rows=c.execute('''SELECT e.id,e.opened_at,e.title,e.side,e.entry_yes,e.early_score,e.reasons_json,e.volume_change,e.liquidity_change,o.roi
          FROM entry_discovery_candidates e JOIN entry_discovery_outcomes o ON o.candidate_id=e.id
          WHERE o.checkpoint_minutes=? ORDER BY e.opened_at,e.id''',(cp,)).fetchall()
        frozen=c.execute('''SELECT f.tier,f.eligible,o.roi FROM entry_discovery_v2_frozen f
          JOIN entry_discovery_outcomes o ON o.candidate_id=f.candidate_id WHERE f.version=? AND o.checkpoint_minutes=?''',(VERSION,cp)).fetchall()
        launch=c.execute("SELECT value FROM entry_discovery_intel_meta WHERE key='launch_at'").fetchone()[0]
    seg=defaultdict(list)
    enriched=[]
    for r in rows:
        _,opened,title,side,price,early,reasons,vol,liq,roi=r
        try: rs=set(json.loads(reasons or '[]'))
        except: rs=set()
        cat=classify_category(title).replace('₿ ','').replace('🤖 ','').replace('🏛 ','').replace('⚽ ','').replace('📦 ','').replace('📈 ','')
        p=float(price); es=float(early); v=float(vol or 0); l=float(liq or 0); y=float(roi)
        keys=[f'Side={side}',f'Category={cat}',f"Price={'<5¢' if p<.05 else '5–20¢' if p<.20 else '20–50¢' if p<.50 else '≥50¢'}",f"Early={'80+' if es>=80 else '70–79' if es>=70 else '60–69' if es>=60 else '<60'}"]
        keys += [x for x in ('PRICE_ALIGNMENT','PRICE_ACCELERATION','PRE_BREAKOUT_MOVE','VOLUME_BUILD','LIQUIDITY_BUILD') if x in rs]
        for k in keys:seg[k].append(y)
        # selected two-feature combinations
        for a in keys:
            for b in keys:
                if a<b and (a.startswith(('Category=','Side=','Price=','Early=')) or b.startswith(('VOLUME_','LIQUIDITY_','PRE_'))): seg[a+' × '+b].append(y)
        enriched.append((opened,y,keys))
    # chronological 60/20/20; rank only zones with n>=25 and positive full mean
    n=len(enriched); a=int(n*.6); b=int(n*.8)
    candidates=[]
    for k,vals in seg.items():
        if len(vals)<25:continue
        # reconstruct by chronology for membership
        d=[];v=[];h=[]
        for i,(_,y,keys) in enumerate(enriched):
            if k in keys or (' × ' in k and all(x in keys for x in k.split(' × '))):
                (d if i<a else v if i<b else h).append(y)
        if len(d)>=15 and sum(d)/len(d)>0:
            candidates.append((sum(d)/len(d),k,_stats(d),_stats(v),_stats(h)))
    candidates.sort(reverse=True,key=lambda x:x[0])
    tiers=defaultdict(list); elig=[]
    for tier,e,roi in frozen:
        tiers[tier].append(float(roi))
        if e:elig.append(float(roi))
    return {'cp':cp,'n':len(rows),'zones':candidates[:8],'tiers':{k:_stats(v) for k,v in tiers.items()},'eligible':_stats(elig),'frozen_n':len(frozen),'launch':launch}

def format_entry_intelligence_report(r):
    label={60:'1ч',180:'3ч',360:'6ч',720:'12ч',1440:'24ч'}[r['cp']]
    lines=[f'🧠 Entry Discovery Intelligence v2 · {label}',f"EARLY outcomes: {r['n']} · chronological 60/20/20",'', '🔬 Walk-Forward · зоны, положительные на Discovery']
    if not r['zones']:lines.append('• Подходящих зон пока нет')
    for _,k,d,v,h in r['zones']:
        state='✅ STABLE' if v['n']>=10 and h['n']>=10 and (v['roi'] or -999)>0 and (h['roi'] or -999)>0 else '⚠️ UNCONFIRMED'
        lines.append(f"• {k} · {state}\n  D {_fmt(d)} · V {_fmt(v)} · H {_fmt(h)}")
    lines += ['', '🧪 EARLY v2 Challenger · FUTURE-ONLY',f"Frozen outcomes: {r['frozen_n']} · eligible score≥70: {_fmt(r['eligible'])}"]
    for tier in ('80+','70-79','60-69','<60'):
        if tier in r['tiers']:lines.append(f"• Score {tier}: {_fmt(r['tiers'][tier])}")
    lines += ['', 'ℹ️ v1 thresholds не меняются. Challenger фиксируется только для кандидатов после запуска v2; historical Walk-Forward не влияет на Live/Trade v2/v3.']
    return '\n'.join(lines)
