"""Outcome-Based Recalibration v2 — diagnostic only.
Uses completed AI Memory checkpoints; never changes live scores or routing.
"""
from __future__ import annotations
import json, math
from collections import defaultdict
from contextlib import closing
from typing import Any, Callable
from database import get_connection
from result_normalization import normalized_training_return, entry_price_bucket
from category_intelligence import classify_category

MIN_SAMPLES=30

def _f(v,default=0.0):
    try: x=float(v); return x if math.isfinite(x) else default
    except: return default

def _bucket(v,cuts,labels):
    x=_f(v)
    for cut,label in zip(cuts,labels):
        if x<cut:return label
    return labels[-1]

def _cat(x):
    s=str(x or '').upper()
    for k in ('CRYPTO','AI/TECH','POLITICS','SPORTS','ETF'):
        if k in s:return k
    return 'OTHER'

def _rows(minutes:int,limit:int=10000):
    with closing(get_connection()) as c:
        raw=c.execute('''SELECT s.entry_price,s.liquidity,s.category,s.title,s.base_score,s.ai_quality,s.ai_risk,s.ml_probability,s.alert_type,s.metadata_json,o.directional_return_percent,o.status
        FROM signal_outcomes o JOIN ai_signals s ON s.signal_id=o.signal_id
        WHERE o.checkpoint_minutes=? AND o.status IS NOT NULL AND o.directional_return_percent IS NOT NULL
        ORDER BY s.created_at DESC LIMIT ?''',(minutes,limit)).fetchall()
    out=[]
    for r in raw:
        try:m=json.loads(r[9] or '{}')
        except:m={}
        out.append({'price':r[0],'liq':r[1],'category':classify_category(str(r[3] or '')),'score':r[4],'q':r[5],'risk':r[6],'ml':r[7],'direction':'DIP' if 'DIP' in str(r[8]).upper() else 'PUMP','m':m,'ret':float(r[10]),'status':str(r[11])})
    return out

def _dimensions():
    return {
      'Price':lambda r:entry_price_bucket(r['price']),
      'Score':lambda r:_bucket(r['score'],[40,60,75,85],['<40','40–59','60–74','75–84','85+']),
      'AI Quality':lambda r:_bucket(r['q'],[40,60,75],['<40','40–59','60–74','75+']),
      'AI Risk':lambda r:_bucket(r['risk'],[30,50,70],['<30','30–49','50–69','70+']),
      'ML':lambda r:_bucket((_f(r['ml'])*100 if 0<=_f(r['ml'])<=1 else _f(r['ml'])),[15,25,40],['<15','15–24','25–39','40+']),
      'Similarity':lambda r:_bucket(r['m'].get('similarity_average'),[70,80,90],['<70','70–79','80–89','90+']),
      'Volume Δ':lambda r:_bucket(r['m'].get('volume_change_percent'),[20,80,300],['<20','20–79','80–299','300+']),
      'Liquidity Δ':lambda r:_bucket(r['m'].get('liquidity_change_percent'),[0,30,100],['<0','0–29','30–99','100+']),
      'Category':lambda r:_cat(r['category']), 'Direction':lambda r:r['direction']}

def _stat(items):
    vals=[normalized_training_return(x['ret']) for x in items]; n=len(vals)
    avg=sum(vals)/n if n else 0; strong=sum(x['status']=='SUCCESS' for x in items)/n*100 if n else 0
    # shrink toward zero so tiny groups cannot top the ranking.
    adj=avg*(n/(n+100.0))
    return {'n':n,'avg':avg,'adj':adj,'strong':strong}

def get_outcome_recalibration_report(minutes:int=180,limit:int=10000):
    rows=_rows(minutes,limit); factors=[]
    for name,fn in _dimensions().items():
        groups=defaultdict(list)
        for r in rows: groups[fn(r)].append(r)
        stats=[(label,_stat(items)) for label,items in groups.items() if len(items)>=MIN_SAMPLES]
        stats.sort(key=lambda x:x[1]['adj'],reverse=True)
        if stats: factors.append({'name':name,'best':stats[0],'worst':stats[-1]})
    return {'minutes':minutes,'n':len(rows),'factors':factors,'category_mode':'v2 title reclassification'}

def format_outcome_recalibration_report(r):
    lines=[f"🧮 Outcome Recalibration v2 · {int(r['minutes']/60)}ч",f"AI Memory samples: {r['n']} · min bucket n={MIN_SAMPLES}",f"Category: {r.get('category_mode','stored')}","","Лучшие / худшие зоны · normalized outcome"]
    for x in r['factors']:
        bl,bs=x['best']; wl,ws=x['worst']
        lines.append(f"• {x['name']}: 🟢 {bl} n={bs['n']} adj {bs['adj']:+.1f}% · 🔴 {wl} n={ws['n']} adj {ws['adj']:+.1f}%")
    lines += ["","ℹ️ Shadow analytics: коэффициенты live scoring не меняются. adj = shrinkage к 0 для защиты от маленьких выборок."]
    return '\n'.join(lines)
