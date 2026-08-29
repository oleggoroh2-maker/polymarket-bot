"""Trade Intelligence / Risk Position Engine v1 — shadow sizing.

Scores entry quality and chase risk, then proposes a paper-only position size.
It never blocks Telegram delivery and never places real orders.
"""
from __future__ import annotations
import math
from typing import Any
from market_regime_engine import classify_market_regime


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError): return default

def _pct(v: Any) -> float | None:
    if v is None: return None
    x=_num(v, float('nan'))
    if not math.isfinite(x): return None
    return x*100.0 if 0 <= x <= 1 else x

def _clamp(x: float, lo: float=0.0, hi: float=100.0)->float: return max(lo,min(hi,x))

def calculate_trade_intelligence(alert: dict[str,Any])->dict[str,Any]:
    final=_clamp(_num(alert.get('final_signal_score'),50))
    ev=_num(alert.get('ev_estimate_percent'))
    base_risk=_clamp(_num(alert.get('risk_score'),50))
    ai_risk=_clamp(_num(alert.get('ai_risk'),50))
    sim=_pct(alert.get('similarity_score'))
    if sim is None: sim=_pct(alert.get('similarity'))
    ml=_pct(alert.get('ml_probability'))
    liq=max(0,_num(alert.get('liquidity')))
    price=_num(alert.get('current_price',alert.get('price')))
    move=abs(_num(alert.get('change_percent')))
    vol=abs(_num(alert.get('volume_change_percent')))
    liq_ch=_num(alert.get('liquidity_change_percent'))
    spread=_num(alert.get('spread'))
    spread_pct=(spread/price*100) if spread>0 and price>0 else 0
    regime=classify_market_regime(alert)

    # Chase risk: large pre-entry move without enough flow/structure confirmation.
    chase=move*0.85
    if move>=40: chase+=10
    if move>=70: chase+=12
    if vol>=80: chase-=12
    elif vol>=30: chase-=5
    elif move>=25: chase+=8
    if liq_ch>=20: chase-=5
    if spread_pct>=5: chase+=10
    if regime['regime']=='EVENT_SHOCK': chase+=15
    elif regime['regime']=='CHAOS_MANIPULATION': chase+=7
    elif regime['regime']=='MOMENTUM': chase-=8
    chase=_clamp(chase)

    # Entry Quality is deliberately interpretable rather than ML-trained.
    q=50.0 + (final-65)*0.55 + ev*1.25 - (base_risk-45)*0.30 - (ai_risk-45)*0.12
    if sim is not None: q+=(sim-70)*0.12
    if ml is not None: q+=(ml-25)*0.06
    if liq>=1_000_000: q+=5
    elif liq>=250_000: q+=3
    elif liq<10_000: q-=10
    if 0.05<=price<=0.80: q+=3
    elif 0<price<0.01 or price>0.95: q-=7
    if spread_pct>=8: q-=10
    elif spread_pct>=4: q-=5
    q-=max(0,chase-35)*0.28
    if regime['regime']=='MOMENTUM': q+=4
    elif regime['regime']=='EVENT_SHOCK': q-=10
    # CHAOS is intentionally near-neutral: early live paper data was not negative.
    elif regime['regime']=='CHAOS_MANIPULATION': q-=1
    q=_clamp(q)

    # Paper-only discrete sizing. Capital is not actually reserved.
    if q>=82 and chase<=45 and base_risk<=55: stake=150.0
    elif q>=72 and chase<=60: stake=100.0
    elif q>=62: stake=75.0
    elif q>=52: stake=50.0
    else: stake=25.0
    if regime['regime']=='EVENT_SHOCK': stake=min(stake,50.0)
    if chase>=75: stake=min(stake,25.0)
    elif chase>=60: stake=min(stake,50.0)
    if base_risk>=70: stake=min(stake,25.0)

    return {
        'entry_quality_score':round(q,1),'chase_risk_score':round(chase,1),
        'suggested_stake_usd':stake,'trade_regime':regime['regime'],
        'trade_regime_confidence':regime['confidence'],'trade_intelligence_version':'v1',
    }

def enrich_with_trade_intelligence(alert:dict[str,Any])->dict[str,Any]:
    try: return {**alert,**calculate_trade_intelligence(alert)}
    except Exception: return {**alert,'entry_quality_score':50.0,'chase_risk_score':50.0,'suggested_stake_usd':50.0,'trade_intelligence_version':'v1-error'}
