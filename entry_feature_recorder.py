"""Entry Feature Recorder v1 — future-only frozen features for EARLY candidates.

Shadow/Paper only. Captures raw pre-entry state and later joins it to existing
Entry Discovery outcomes. It never filters, scores, routes, or trades.
"""
from __future__ import annotations
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone, timedelta
from typing import Any
import json, math
import requests

from database import get_connection
from category_intelligence import classify_category
from market_structure import CLOB_BOOKS_URL, _book_metrics

VERSION = "v1-entry-feature-recorder"
BOOK_TIMEOUT = 5


def _now(): return datetime.now(timezone.utc)
def _f(v, default=None):
    try: return float(v) if v is not None else default
    except (TypeError, ValueError): return default

def _arr(v):
    if isinstance(v, list): return v
    if isinstance(v, tuple): return list(v)
    if isinstance(v, str):
        try:
            x=json.loads(v); return x if isinstance(x,list) else []
        except Exception:return []
    return []

def ensure_schema():
    with closing(get_connection()) as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS entry_feature_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS entry_feature_snapshots(
          candidate_id INTEGER PRIMARY KEY, captured_at TEXT NOT NULL, version TEXT NOT NULL,
          category_v2 TEXT, side TEXT, entry_yes REAL, early_score REAL,
          change_5m REAL, change_15m REAL, change_1h REAL, change_24h REAL,
          price_velocity_5m REAL, price_velocity_15m REAL, price_velocity_1h REAL,
          acceleration REAL, volume_change REAL, liquidity_change REAL,
          distance_1h_low_pct REAL, distance_1h_high_pct REAL,
          same_direction_count INTEGER, move_age_minutes REAL,
          best_bid REAL, best_ask REAL, spread REAL, bid_depth REAL, ask_depth REAL,
          bid_balance REAL, largest_order REAL, depth_imbalance REAL,
          reasons_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_entry_feature_time ON entry_feature_snapshots(captured_at);
        ''')
        row=c.execute("SELECT value FROM entry_feature_meta WHERE key='launch_at'").fetchone()
        if not row:
            c.execute("INSERT INTO entry_feature_meta(key,value) VALUES('launch_at',?)",(_now().isoformat(),))
        c.commit()

def _yes_token(m):
    outcomes=[str(x).lower() for x in _arr(m.get('outcomes'))]
    toks=[str(x) for x in _arr(m.get('clob_token_ids'))]
    for i,name in enumerate(outcomes):
        if name=='yes' and i<len(toks): return toks[i]
    return toks[0] if toks else None

def _history_extrema(c, mid, now):
    cutoff=(now-timedelta(hours=1)).isoformat()
    rows=c.execute("SELECT price FROM prices WHERE id=? AND timestamp>=?",(mid,cutoff)).fetchall()
    vals=[float(r[0]) for r in rows if r and r[0] is not None]
    return (min(vals),max(vals)) if vals else (None,None)

def _move_age(m):
    # Conservative estimate from available aligned horizons; frozen, not inferred later.
    vals=[(5,_f(m.get('change_5m'))),(15,_f(m.get('change_15m'))),(60,_f(m.get('change_1h')))]
    lead=next((v for _,v in vals if v is not None and abs(v)>=1.0),None)
    if lead is None:return None
    sign=1 if lead>0 else -1
    aligned=[mins for mins,v in vals if v is not None and abs(v)>=1.0 and (1 if v>0 else -1)==sign]
    return max(aligned) if aligned else None

def record_candidates(items: list[tuple[int,dict[str,Any]]]) -> int:
    """Freeze raw features for newly inserted EARLY candidates. One batched CLOB request."""
    ensure_schema()
    if not items:return 0
    books={}
    token_rows=[]
    for cid,m in items:
        tok=_yes_token(m)
        if tok: token_rows.append((cid,tok))
    if token_rows:
        try:
            r=requests.post(CLOB_BOOKS_URL,json=[{'token_id':t} for _,t in token_rows],timeout=BOOK_TIMEOUT)
            r.raise_for_status(); payload=r.json()
            if isinstance(payload,list):
                for (cid,_),book in zip(token_rows,payload):
                    if isinstance(book,dict): books[cid]=_book_metrics(book)
        except Exception:
            books={}
    now=_now(); added=0; added_ids=[]
    with closing(get_connection()) as c:
        for cid,m in items:
            if c.execute("SELECT 1 FROM entry_feature_snapshots WHERE candidate_id=?",(cid,)).fetchone():continue
            p=_f(m.get('price'),0.0); lo,hi=_history_extrema(c,str(m.get('id') or ''),now)
            dlow=((p-lo)/lo*100) if lo and lo>0 else None
            dhigh=((p-hi)/hi*100) if hi and hi>0 else None
            c5=_f(m.get('change_5m')); c15=_f(m.get('change_15m')); c1=_f(m.get('change_1h')); c24=_f(m.get('change_24h'))
            # Comparable %/minute velocities; acceleration = short velocity - medium velocity.
            v5=c5/5 if c5 is not None else None; v15=c15/15 if c15 is not None else None; v1=c1/60 if c1 is not None else None
            accel=(v5-v15) if v5 is not None and v15 is not None else None
            present=[x for x in (c5,c15,c1) if x is not None and abs(x)>=1.0]
            sign=(1 if present[0]>0 else -1) if present else 0
            same=sum(1 for x in present if (1 if x>0 else -1)==sign)
            b=books.get(cid,{})
            bd=_f(b.get('bid_depth')); ad=_f(b.get('ask_depth'))
            imb=((bd-ad)/(bd+ad)*100) if bd is not None and ad is not None and bd+ad>0 else None
            row=c.execute("SELECT side,early_score,reasons_json FROM entry_discovery_candidates WHERE id=?",(cid,)).fetchone()
            if not row:continue
            side,early,reasons=row
            cat=classify_category(str(m.get('title') or ''))
            c.execute('''INSERT OR IGNORE INTO entry_feature_snapshots
              (candidate_id,captured_at,version,category_v2,side,entry_yes,early_score,change_5m,change_15m,change_1h,change_24h,
               price_velocity_5m,price_velocity_15m,price_velocity_1h,acceleration,volume_change,liquidity_change,
               distance_1h_low_pct,distance_1h_high_pct,same_direction_count,move_age_minutes,best_bid,best_ask,spread,bid_depth,ask_depth,bid_balance,largest_order,depth_imbalance,reasons_json)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
              (cid,now.isoformat(),VERSION,cat,side,p,early,c5,c15,c1,c24,v5,v15,v1,accel,_f(m.get('volume_change_1h'),_f(m.get('volume_change_15m'))),_f(m.get('liquidity_change_1h'),_f(m.get('liquidity_change_15m'))),dlow,dhigh,same,_move_age(m),_f(b.get('best_bid')),_f(b.get('best_ask')),_f(b.get('spread')),bd,ad,_f(b.get('bid_balance')),_f(b.get('largest_order')),imb,reasons))
            added+=1; added_ids.append(int(cid))
        c.commit()
    if added_ids:
        try:
            from entry_feature_intelligence_v2 import freeze_feature_candidates
            freeze_feature_candidates(added_ids)
        except Exception:
            pass
    return added

def _stats(vals):
    vals=[float(x) for x in vals]; n=len(vals)
    if not n:return {'n':0,'roi':None,'pf':None}
    gp=sum(max(x,0) for x in vals); gl=-sum(min(x,0) for x in vals)
    return {'n':n,'roi':sum(vals)/n,'pf':gp/gl if gl else (float('inf') if gp else None)}

def _fmt(s):
    roi='—' if s['roi'] is None else f"{s['roi']:+.1f}%"; pf='—' if s['pf'] is None else ('∞' if math.isinf(s['pf']) else f"{s['pf']:.2f}")
    return f"n={s['n']} ROI {roi} PF {pf}"

def get_feature_report(cp=360):
    ensure_schema()
    with closing(get_connection()) as c:
        total=c.execute("SELECT COUNT(*) FROM entry_feature_snapshots WHERE version=?",(VERSION,)).fetchone()[0]
        rows=c.execute('''SELECT f.side,f.entry_yes,f.early_score,f.acceleration,f.volume_change,f.liquidity_change,
          f.distance_1h_low_pct,f.distance_1h_high_pct,f.spread,f.bid_balance,f.depth_imbalance,f.same_direction_count,o.roi
          FROM entry_feature_snapshots f JOIN entry_discovery_outcomes o ON o.candidate_id=f.candidate_id
          WHERE f.version=? AND o.checkpoint_minutes=? ORDER BY f.captured_at''',(VERSION,cp)).fetchall()
    seg=defaultdict(list)
    for side,p,early,acc,vol,liq,dlow,dhigh,spread,bal,imb,same,roi in rows:
        keys=[f"Side={side}",f"Price={'<5¢' if p<.05 else '5–20¢' if p<.20 else '20–50¢' if p<.50 else '≥50¢'}",f"Early={'80+' if early>=80 else '70–79' if early>=70 else '<70'}"]
        if acc is not None: keys.append('Acceleration=UP' if acc>0.05 else 'Acceleration=FLAT/DOWN')
        if vol is not None: keys.append('Volume=80+' if vol>=80 else 'Volume=<80')
        if liq is not None: keys.append('LiquidityΔ=30+' if liq>=30 else 'LiquidityΔ=<30')
        if spread is not None: keys.append('Spread=<2¢' if spread<.02 else 'Spread=2–5¢' if spread<.05 else 'Spread=5¢+')
        if bal is not None: keys.append('BidBalance=40%+' if bal>=40 else 'BidBalance=<40%')
        if same is not None: keys.append(f'Alignment={int(same)}')
        for k in keys:seg[k].append(float(roi))
        # Small pre-registered diagnostic interactions, not a strategy.
        for a in keys:
            for b in keys:
                if a<b and (a.startswith(('Spread=','BidBalance=','Acceleration=')) or b.startswith(('Spread=','BidBalance=','Acceleration='))):
                    seg[a+' × '+b].append(float(roi))
    ranked=[(k,_stats(v)) for k,v in seg.items() if len(v)>=15]
    ranked.sort(key=lambda x:(x[1]['roi'] if x[1]['roi'] is not None else -999),reverse=True)
    return {'cp':cp,'total':int(total or 0),'outcomes':len(rows),'best':ranked[:8],'worst':sorted(ranked,key=lambda x:x[1]['roi'] if x[1]['roi'] is not None else 999)[:5]}

def format_feature_report(r):
    label={60:'1ч',180:'3ч',360:'6ч',720:'12ч',1440:'24ч'}[r['cp']]
    lines=[f'🧬 Entry Feature Recorder v1 · {label}',f"Frozen snapshots: {r['total']} · outcomes: {r['outcomes']}",'', '🔎 Лучшие feature-срезы · min n=15']
    if not r['best']:lines.append('• Пока недостаточно данных')
    else:
        for k,s in r['best']:lines.append(f'• {k} · {_fmt(s)}')
    if r['worst']:
        lines += ['', '⚠️ Худшие feature-срезы']
        for k,s in r['worst']:lines.append(f'• {k} · {_fmt(s)}')
    lines += ['', 'ℹ️ Future-only recorder: признаки заморожены в момент EARLY. Это диагностика, не фильтр; Entry v1/v2 и Live не меняются.']
    return '\n'.join(lines)
