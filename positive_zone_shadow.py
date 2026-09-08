"""Positive Zone Shadow Trader — future-only validation of historical zones.

Membership is frozen when a delivered Paper signal is recorded. No live routing,
Trade v2/v3 decision, stake, or score is changed by this module.
"""
from __future__ import annotations
import json, math
from contextlib import closing
from typing import Any
from database import get_connection
from category_intelligence import classify_category

VERSION = "v1-frozen"
CHECKPOINTS = ((60,"1ч"),(180,"3ч"),(360,"6ч"),(720,"12ч"),(1440,"24ч"))

ZONE_LABELS = {
    "CRYPTO_SIM_70_79": "CRYPTO × Similarity 70–79",
    "CRYPTO_VOLUME_300_PLUS": "CRYPTO × Volume Δ 300+",
    "LOW_SCORE_SIM_70_79": "Score <40 × Similarity 70–79",
    "CRYPTO_LIQ_100_PLUS": "CRYPTO × Liquidity Δ 100+",
    "HIGH_PRICE_CRYPTO": "Price ≥50¢ × CRYPTO",
    "MID_PRICE_CRYPTO": "Price 20–50¢ × CRYPTO",
}

def _num(v, default=None):
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:return default

def classify_positive_zones(alert:dict[str,Any])->list[str]:
    title=str(alert.get("title") or alert.get("question") or "")
    cat=classify_category(title)
    sim=_num(alert.get("similarity_average"))
    vol=_num(alert.get("volume_change_percent"))
    liq=_num(alert.get("liquidity_change_percent"))
    score=_num(alert.get("score", alert.get("base_score")))
    price=_num(alert.get("current_price",alert.get("price")))
    out=[]
    crypto="CRYPTO" in cat.upper()
    sim7079=sim is not None and 70<=sim<80
    if crypto and sim7079: out.append("CRYPTO_SIM_70_79")
    if crypto and vol is not None and vol>=300: out.append("CRYPTO_VOLUME_300_PLUS")
    if score is not None and score<40 and sim7079: out.append("LOW_SCORE_SIM_70_79")
    if crypto and liq is not None and liq>=100: out.append("CRYPTO_LIQ_100_PLUS")
    if crypto and price is not None and price>=0.50: out.append("HIGH_PRICE_CRYPTO")
    if crypto and price is not None and 0.20<=price<0.50: out.append("MID_PRICE_CRYPTO")
    return out

def _side_price(yes,side):
    p=min(.999999,max(.000001,float(yes))); return 1-p if side=="NO" else p

def _pnl(entry,exitp,side,stake=100.0,cost_pct=1.0):
    e=_side_price(entry,side); x=_side_price(exitp,side); shares=stake/e
    return shares*(x-e)-stake*cost_pct/100.0

def get_zone_shadow_report()->dict[str,Any]:
    with closing(get_connection()) as c:
        try:
            rows=c.execute("""SELECT signal_id,entry_price,trade_side,alert_type,positive_zones_json
                FROM paper_trades WHERE positive_zone_version=? AND positive_zones_json IS NOT NULL
                AND positive_zones_json<>'' AND positive_zones_json<>'[]'""",(VERSION,)).fetchall()
        except Exception:
            rows=[]
        memberships={}
        base={}
        for sid,entry,side,atype,raw in rows:
            try: zones=json.loads(raw or '[]')
            except Exception: zones=[]
            base[sid]=(float(entry),str(side or ("NO" if "DIP" in str(atype).upper() else "YES")))
            for z in zones: memberships.setdefault(str(z),set()).add(str(sid))
        result=[]
        for zid,sids in memberships.items():
            item={"id":zid,"label":ZONE_LABELS.get(zid,zid),"signals":len(sids),"checkpoints":{}}
            for minute,label in CHECKPOINTS:
                if not sids: continue
                q=','.join('?' for _ in sids)
                cps=c.execute(f"SELECT signal_id,price FROM signal_outcomes WHERE checkpoint_minutes=? AND status IS NOT NULL AND signal_id IN ({q})",(minute,*sids)).fetchall()
                pnls=[]
                for sid,exitp in cps:
                    entry,side=base[str(sid)]; pnls.append(_pnl(entry,float(exitp),side))
                gp=sum(x for x in pnls if x>0); gl=-sum(x for x in pnls if x<0)
                item["checkpoints"][label]={"n":len(pnls),"pnl":sum(pnls),"roi":sum(pnls)/(100*len(pnls))*100 if pnls else None,"win":sum(x>0 for x in pnls)/len(pnls)*100 if pnls else None,"pf":gp/gl if gl else (float('inf') if gp else None)}
            result.append(item)
        result.sort(key=lambda x:(x["signals"],x["id"]),reverse=True)
    return {"version":VERSION,"zones":result,"signals":len(rows)}

def format_zone_shadow_report(r):
    lines=["🧪 Positive Zone Shadow · FUTURE-ONLY",f"Frozen signals with zone: {r.get('signals',0)} · {r.get('version',VERSION)}",""]
    if not r.get('zones'):
        return '\n'.join(lines+["Пока нет новых доставленных сигналов, попавших в выбранные зоны.","","ℹ️ Shadow only: live routing и Trade v2/v3 не изменяются."])
    for z in r['zones']:
        lines.append(f"• {z['label']} · signals {z['signals']}")
        bits=[]
        for label in ("3ч","24ч"):
            x=z['checkpoints'].get(label,{})
            roi='—' if x.get('roi') is None else f"{x['roi']:+.1f}%"
            pf='—' if x.get('pf') is None else ('∞' if x.get('pf')==float('inf') else f"{x['pf']:.2f}")
            bits.append(f"{label} n={x.get('n',0)} ROI {roi} PF {pf}")
        lines.append("  "+" · ".join(bits))
    lines += ["","ℹ️ Membership фиксируется при входе. $100 side-aware counterfactual, cost 1%. Shadow only."]
    return '\n'.join(lines)
