"""Score Calibration Audit v1.0 — Shadow Mode.

Audits whether higher base Score actually corresponds to better 24h outcomes,
and exposes context-dependent reversals (price/category/liquidity/similarity).
Diagnostic only: it never changes alerts, weights, or confidence.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from contextlib import closing
from typing import Any

import config
from database import get_connection
from result_normalization import entry_price_bucket, normalized_training_return

SCORE_BUCKETS = [("<40", None, 40), ("40–59", 40, 60), ("60–74", 60, 75), ("75–84", 75, 85), ("85+", 85, None)]
LIQ_BUCKETS = [("<$10k", None, 10_000), ("$10–50k", 10_000, 50_000), ("$50–250k", 50_000, 250_000), ("$250k–1M", 250_000, 1_000_000), ("$1M+", 1_000_000, None)]
SIM_BUCKETS = [("<60%", None, 60), ("60–69%", 60, 70), ("70–79%", 70, 80), ("80–89%", 80, 90), ("90%+", 90, None)]


def _num(v: Any, default: float = 0.0) -> float:
    try: x = float(v)
    except (TypeError, ValueError): return default
    return x if math.isfinite(x) else default


def _bucket(v: float | None, cuts):
    if v is None: return None
    for label, lo, hi in cuts:
        if lo is not None and v < lo: continue
        if hi is not None and v >= hi: continue
        return label
    return None


def _meta(raw):
    try: x = json.loads(raw or "{}")
    except Exception: return {}
    return x if isinstance(x, dict) else {}


def _load(checkpoint: int, limit: int):
    with closing(get_connection()) as con:
        rows = con.execute("""
            SELECT s.base_score, s.entry_price, s.liquidity, s.category,
                   s.alert_type, s.metadata_json, o.status, o.directional_return_percent
            FROM ai_signals s JOIN signal_outcomes o ON o.signal_id=s.signal_id
            WHERE o.checkpoint_minutes=? AND o.status IS NOT NULL
            ORDER BY o.measured_at DESC LIMIT ?
        """, (checkpoint, limit)).fetchall()
    out=[]
    for r in rows:
        m=_meta(r[5]); sim=m.get("similarity_average")
        if sim is not None:
            sim=_num(sim)
            if 0 <= sim <= 1: sim *= 100
        typ=str(r[4] or "").upper()
        out.append({
            "score": _bucket(_num(r[0]), SCORE_BUCKETS),
            "price": entry_price_bucket(r[1]),
            "liquidity": _bucket(max(0,_num(r[2])), LIQ_BUCKETS),
            "category": str(r[3] or "OTHER").upper(),
            "similarity": _bucket(sim, SIM_BUCKETS),
            "direction": "DIP" if "DIP" in typ else ("OPPORTUNITY" if "OPPORTUNITY" in typ else "PUMP"),
            "strong": str(r[6] or "").upper()=="SUCCESS",
            "continued": str(r[6] or "").upper() in {"SUCCESS","PARTIAL"},
            "ret": normalized_training_return(r[7]),
        })
    return out


def _stats(rows):
    n=len(rows)
    if not n: return {"n":0,"strong":0.0,"continued":0.0,"ret":0.0}
    return {"n":n,"strong":100*sum(x["strong"] for x in rows)/n,
            "continued":100*sum(x["continued"] for x in rows)/n,
            "ret":sum(x["ret"] for x in rows)/n}


def get_calibration_audit_report(checkpoint_minutes=None, max_rows=None):
    cp=int(checkpoint_minutes or getattr(config,"CALIBRATION_AUDIT_CHECKPOINT_MINUTES",1440))
    limit=int(max_rows or getattr(config,"CALIBRATION_AUDIT_MAX_ROWS",5000))
    min_ctx=int(getattr(config,"CALIBRATION_AUDIT_MIN_CONTEXT_SAMPLES",30))
    rows=_load(cp,limit); base=_stats(rows)
    score=[]
    for label,_,__ in SCORE_BUCKETS:
        s=_stats([x for x in rows if x["score"]==label]); s["label"]=label; score.append(s)

    contexts=[]
    for score_label,_,__ in SCORE_BUCKETS:
        sr=[x for x in rows if x["score"]==score_label]
        for dim in ("price","liquidity","category","similarity","direction"):
            vals=sorted({x[dim] for x in sr if x.get(dim) is not None})
            for val in vals:
                st=_stats([x for x in sr if x.get(dim)==val])
                if st["n"] < min_ctx: continue
                st.update({"score":score_label,"dimension":dim,"value":val,
                           "strong_delta":st["strong"]-base["strong"],
                           "return_delta":st["ret"]-base["ret"]})
                contexts.append(st)
    # Rank by a conservative edge; n reliability prevents tiny buckets dominating.
    shrink=float(getattr(config,"CALIBRATION_AUDIT_SHRINKAGE_SAMPLES",100))
    for x in contexts:
        rel=x["n"]/(x["n"]+shrink)
        x["edge"]=(0.75*x["strong_delta"]+0.25*x["return_delta"])*rel
    best=sorted(contexts,key=lambda x:x["edge"],reverse=True)[:8]
    worst=sorted(contexts,key=lambda x:x["edge"])[:8]

    # Monotonicity audit: count adjacent score buckets where Strong fails to rise.
    populated=[x for x in score if x["n"]>=min_ctx]
    violations=[]
    for a,b in zip(populated,populated[1:]):
        if b["strong"] + 0.25 < a["strong"]:
            violations.append(f'{a["label"]} → {b["label"]}: {a["strong"]:.1f}% → {b["strong"]:.1f}%')
    return {"total":len(rows),"base":base,"score_buckets":score,"best":best,"worst":worst,
            "violations":violations,"shadow_mode":True}


def _ctx(x):
    names={"price":"Цена","liquidity":"Ликвидность","category":"Категория","similarity":"Similarity","direction":"Направление"}
    return f'Score {x["score"]} + {names.get(x["dimension"],x["dimension"])} {x["value"]}'


def format_calibration_audit_report(r):
    if not r.get("total"):
        return "🎚 Score Calibration Audit · Shadow Mode\n\nПока нет проверенных 24ч сигналов."
    b=r["base"]
    lines=["🎚 Score Calibration Audit · Shadow Mode","",f'Проверено сигналов: {r["total"]}',
           "⚠️ Аудит ничего не меняет в реальных алертах.","",
           f'📊 База: Strong {b["strong"]:.1f}% · Любое {b["continued"]:.1f}% · Норм. {b["ret"]:+.1f}%','',
           "🎯 Калибровка Score"]
    for x in r["score_buckets"]:
        if x["n"]:
            lines.append(f'• {x["label"]}: Strong {x["strong"]:.1f}% · Любое {x["continued"]:.1f}% · Норм. {x["ret"]:+.1f}% (n={x["n"]})')
    lines += ["", "🚨 Нарушения монотонности"]
    if r["violations"]:
        lines += [f"• {v}" for v in r["violations"]]
    else: lines.append("• Не обнаружены на достаточной выборке")
    lines += ["", "🟢 Сильные контексты Score"]
    for i,x in enumerate(r["best"][:5],1):
        lines.append(f'{i}. {_ctx(x)}\n   Strong {x["strong"]:.1f}% ({x["strong_delta"]:+.1f} п.п.) · Норм. {x["ret"]:+.1f}% · n={x["n"]}')
    lines += ["", "🔴 Слабые контексты Score"]
    for i,x in enumerate(r["worst"][:5],1):
        lines.append(f'{i}. {_ctx(x)}\n   Strong {x["strong"]:.1f}% ({x["strong_delta"]:+.1f} п.п.) · Норм. {x["ret"]:+.1f}% · n={x["n"]}')
    lines += ["", "ℹ️ Цель аудита — проверить, означает ли более высокий Score реально более сильное 24ч продолжение. Автоперекалибровка отключена."]
    return "\n".join(lines)
