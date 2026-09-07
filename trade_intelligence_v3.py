"""Trade Intelligence v3 — shadow-only entry decision.

Uses Final Signal v2 and entry-time evidence. Never changes Telegram routing or
real/live decisions. v2 remains the production comparison baseline.
"""
from __future__ import annotations
import math
from typing import Any


def _n(v: Any, d: float = 0.0) -> float:
    try:
        x=float(v); return x if math.isfinite(x) else d
    except (TypeError, ValueError): return d


def _is_dip(s: str) -> bool:
    s=str(s or '').upper(); return any(k in s for k in ('DIP','DROP','BEAR'))


def calculate_trade_v3(a: dict[str, Any]) -> dict[str, Any]:
    fv2=_n(a.get('final_v2_score'),50); ev=_n(a.get('ev_estimate_percent'),-5)
    cont=_n(a.get('ev_continuation_probability'),20); ml=_n(a.get('ml_probability'),0)
    if 0 <= ml <= 1: ml*=100
    q=_n(a.get('entry_quality_score'),50); chase=_n(a.get('chase_risk_score'),50)
    risk=_n(a.get('risk_score'),50); price=_n(a.get('current_price',a.get('price')),0)
    side='NO' if _is_dip(a.get('alert_type')) else 'YES'; side_price=1-price if side=='NO' and 0<price<1 else price
    ns=str(a.get('news_status') or 'UNKNOWN').upper(); catalyst=str(a.get('news_catalyst_class') or '').upper()
    support=str(a.get('news_outcome_support') or a.get('news_direction') or 'NEUTRAL').upper()
    priced=_n(a.get('news_priced_in_risk'))
    regime=str(a.get('market_regime') or a.get('trade_regime') or '').upper()
    reasons=[]
    if fv2 < 56: reasons.append('FINAL_V2_LOW')
    if ev < -1: reasons.append('EV_NEGATIVE')
    if cont < 30: reasons.append('LOW_CONTINUATION')
    if q < 55: reasons.append('LOW_ENTRY_QUALITY')
    if chase > 70: reasons.append('CHASE_RISK')
    if risk > 70: reasons.append('HIGH_RISK')
    if side_price > .95: reasons.append('POOR_PAYOFF_PRICE')
    if ns=='CONTRADICTED': reasons.append('NEWS_CONTRADICTED')
    if priced >= 75: reasons.append('PRICED_IN')
    if regime=='EVENT_SHOCK' and not (fv2>=75 and ev>=2 and cont>=40): reasons.append('EVENT_SHOCK')
    decision='TRADE' if not reasons else 'SKIP'
    # Frozen paper stake; deliberately modest until v3 proves itself.
    if decision=='TRADE':
        stake=100.0 if fv2>=68 and ev>=1.5 and q>=65 else 75.0 if fv2>=60 else 50.0
    else: stake=0.0
    v2=str(a.get('trade_v2_decision') or '')
    challenger=(v2=='SKIP' and decision=='TRADE')
    disagreement='V2_SKIP_V3_TRADE' if challenger else ('V2_TRADE_V3_SKIP' if v2=='TRADE' and decision=='SKIP' else 'AGREE')
    return {'trade_v3_decision':decision,'trade_v3_skip_reasons':reasons,'trade_v3_stake':stake,
            'trade_v3_version':'v3-shadow','trade_v3_challenger':challenger,'trade_v3_disagreement':disagreement,
            'trade_v3_side_price':round(side_price,6),'trade_v3_news_support':support,'trade_v3_catalyst':catalyst}


def enrich_with_trade_v3(a: dict[str, Any]) -> dict[str, Any]:
    try: return {**a, **calculate_trade_v3(a)}
    except Exception:
        return {**a,'trade_v3_decision':'SKIP','trade_v3_skip_reasons':['ENGINE_ERROR'],'trade_v3_stake':0.0,
                'trade_v3_version':'v3-error','trade_v3_challenger':False,'trade_v3_disagreement':'ERROR'}
