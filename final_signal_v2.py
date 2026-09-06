"""Final Signal Engine v2 — shadow entry-quality score.

Unlike v1, v2 scores the quality of entering *now*, not the magnitude of the
move that already happened. It is analytics-only and never blocks delivery.
"""
from __future__ import annotations
import math
from typing import Any

def _n(v, d=0.0):
    try:
        x=float(v); return x if math.isfinite(x) else d
    except (TypeError,ValueError): return d

def _o(v):
    if v is None:return None
    try:
        x=float(v); return x if math.isfinite(x) else None
    except (TypeError,ValueError):return None

def _pct(v):
    x=_o(v)
    if x is None:return None
    return max(0,min(100,x*100 if 0<=x<=1 else x))

def calculate_final_signal_v2(a:dict[str,Any])->dict[str,Any]:
    ev=_n(a.get('ev_estimate_percent'),-5); cont=_n(a.get('ev_continuation_probability'),20)
    ml=_pct(a.get('ml_probability')); strong=_o(a.get('similarity_strong_rate')); avgret=_o(a.get('similarity_average_return'))
    q=_n(a.get('entry_quality_score'),50); chase=_n(a.get('chase_risk_score'),50); risk=_n(a.get('risk_score'),50)
    ns=str(a.get('news_status') or 'UNKNOWN'); nd=str(a.get('news_direction') or 'NEUTRAL'); nscore=_n(a.get('news_score'))
    catalyst=str(a.get('news_catalyst_class') or ns); support=str(a.get('news_outcome_support') or 'NEUTRAL')
    regime=str(a.get('market_regime') or '')
    change=abs(_n(a.get('change_percent'))); liqchg=_o(a.get('liquidity_change_percent'))
    comps=[]
    def add(k,l,p,v): comps.append({'key':k,'label':l,'points':round(p,2),'value':v})
    # neutral baseline, then evidence about post-entry continuation/payoff
    add('ev','EV',max(-18,min(14,ev*1.8)),ev)
    add('continuation','Continuation',max(-12,min(12,(cont-30)*0.45)),cont)
    if ml is not None:add('ml','ML continuation',max(-9,min(9,(ml-35)*0.28)),ml)
    if strong is not None:add('history','Historical continuation',max(-10,min(10,(strong-25)*0.35)),strong)
    if avgret is not None:add('history_return','Similar avg return',max(-8,min(8,avgret*0.22)),avgret)
    add('entry_quality','Entry Quality',max(-10,min(10,(q-55)*0.30)),q)
    add('chase','Chase Risk',-max(0,(chase-35)*0.22),chase)
    add('risk','Execution Risk',-max(0,(risk-45)*0.12),risk)
    if catalyst=='CONTRADICTED' or ns=='CONTRADICTED': add('news','News contradicted',-16 if nscore>=50 else -10,nscore)
    elif support in ('YES','NO') and catalyst in ('CONFIRMED_NEWS','CONFIRMED_CATALYST'):
        add('news','Confirmed directional catalyst',min(10,3+nscore*.07),nscore)
    elif catalyst=='RUMOR': add('news','Rumor only',-2,nscore)
    elif catalyst=='NO_CATALYST' and change>=50: add('news','Large move without catalyst',-7,change)
    if _n(a.get('news_priced_in_risk'))>=60:add('priced_in','Catalyst likely priced in',-7,_n(a.get('news_priced_in_risk')))
    if regime=='EVENT_SHOCK':add('regime','Event shock',-8,regime)
    elif regime=='CHAOS_MANIPULATION':add('regime','Chaos/manipulation',-5,regime)
    if change>=100:add('late_move','Move already >100%',-8,change)
    elif change>=50:add('late_move','Move already >50%',-4,change)
    if liqchg is not None and liqchg<=-20:add('liq_fade','Liquidity deterioration',-4,liqchg)
    raw=50+sum(float(x['points']) for x in comps)
    # hard caps: a deeply negative payoff cannot be called ELITE.
    cap=100.0
    if ev<=-8:cap=44
    elif ev<=-5:cap=54
    elif ev<=-2:cap=64
    if ns=='CONTRADICTED' and nscore>=50:cap=min(cap,49)
    if chase>=85:cap=min(cap,49)
    score=round(max(0,min(cap,raw)),1)
    tier='ELITE' if score>=80 else 'STRONG' if score>=68 else 'GOOD' if score>=56 else 'WATCH' if score>=44 else 'WEAK'
    return {'final_v2_score':score,'final_v2_tier':tier,'final_v2_components':sorted(comps,key=lambda x:abs(x['points']),reverse=True),'final_v2_cap':cap,'final_v2_version':'v2-shadow','final_v2_live':False}

def enrich_with_final_signal_v2(a):
    try:return {**a,**calculate_final_signal_v2(a)}
    except Exception:return {**a,'final_v2_score':50.0,'final_v2_tier':'WATCH','final_v2_components':[],'final_v2_version':'v2-error','final_v2_live':False}
