"""Expected Value + Risk Engine v1.

Live delivery layer on top of Final Signal Engine.  It does not use future
outcomes or query the training database at decision time: only fields already
attached to the current alert are used.  All candidates are still recorded by
AI Memory before this gate.
"""
from __future__ import annotations

import math
from typing import Any


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _opt(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pct(v: Any) -> float | None:
    x = _opt(v)
    if x is None:
        return None
    return max(0.0, min(100.0, x * 100.0 if 0 <= x <= 1 else x))


def _category(alert: dict[str, Any]) -> str:
    text = str(alert.get("category") or "OTHER").upper()
    if "CRYPTO" in text or "BITCOIN" in text: return "CRYPTO"
    if "AI" in text or "TECH" in text: return "AI/TECH"
    if "SPORT" in text: return "SPORTS"
    if "POLIT" in text: return "POLITICS"
    if "ETF" in text: return "ETF"
    return "OTHER"


def calculate_ev_risk(alert: dict[str, Any]) -> dict[str, Any]:
    final = max(0.0, min(100.0, _num(alert.get("final_signal_score"), 50.0)))
    ai_risk = max(0.0, min(100.0, _num(alert.get("ai_risk"), 50.0)))
    ml = _pct(alert.get("ml_probability"))
    sim_n = int(_num(alert.get("similarity_samples")))
    sim_strong = _opt(alert.get("similarity_strong_rate"))
    sim_any = _opt(alert.get("similarity_continuation_rate"))
    sim_ret = _opt(alert.get("similarity_average_return"))
    liquidity = max(0.0, _num(alert.get("liquidity")))
    spread = _opt(alert.get("spread"))
    if spread is None:
        bid, ask = _opt(alert.get("best_bid")), _opt(alert.get("best_ask"))
        if bid is not None and ask is not None:
            spread = max(0.0, ask - bid)
    price = _num(alert.get("current_price", alert.get("price")))
    change = abs(_num(alert.get("change_percent")))
    category = _category(alert)
    alert_type = str(alert.get("alert_type") or "").upper()

    # Estimated continuation probability. Similar-history evidence is strongest
    # when sufficiently sampled; Final Signal supplies a conservative prior.
    prior = 8.0 + final * 0.32  # 24% at FS50, 40% at FS100
    if sim_n >= 20 and sim_strong is not None:
        hist = max(0.0, min(100.0, sim_strong))
        weight = min(0.65, sim_n / 120.0 * 0.65)
        continuation = prior * (1.0 - weight) + hist * weight
    else:
        continuation = prior
    if sim_any is not None and sim_n >= 20:
        continuation = 0.75 * continuation + 0.25 * max(0.0, min(100.0, sim_any))
    continuation = max(2.0, min(85.0, continuation))

    # Expected move if continuation happens. Prefer live similarity history;
    # otherwise use a restrained fraction of the observed move.
    if sim_ret is not None and sim_n >= 20:
        upside = max(3.0, min(60.0, abs(sim_ret)))
    else:
        upside = max(3.0, min(35.0, change * 0.35))

    # Loss assumption grows with AI risk and deteriorating market structure.
    downside = 7.0 + ai_risk * 0.10
    if liquidity < 10_000: downside += 5.0
    elif liquidity < 50_000: downside += 2.0
    if price > 0.85 or (0 < price < 0.01): downside += 3.0
    if category == "POLITICS": downside += 3.0
    if "OPPORTUNITY" in alert_type: downside += 5.0
    if ml is not None and ml < 15: downside += 2.0
    downside = min(35.0, downside)

    p = continuation / 100.0
    ev = p * upside - (1.0 - p) * downside

    risk = ai_risk * 0.55
    risk += 12.0 if liquidity < 10_000 else 7.0 if liquidity < 50_000 else 2.0 if liquidity < 250_000 else 0.0
    risk += 8.0 if price > 0.85 or (0 < price < 0.01) else 0.0
    risk += 8.0 if category == "POLITICS" else 0.0
    risk += 10.0 if "OPPORTUNITY" in alert_type else 0.0
    risk += 5.0 if ml is not None and ml < 15 else 0.0
    risk -= min(10.0, max(0.0, final - 65.0) * 0.35)
    risk = max(0.0, min(100.0, risk))

    # Live gate: no category is hard-blocked. High Final Signal can pass from
    # every category, while marginal alerts need positive EV and acceptable risk.
    if "OPPORTUNITY" in alert_type:
        passed, reason = False, "OPPORTUNITY"
    elif final >= 82 and risk <= 72:
        passed, reason = True, "ELITE"
    elif final >= 74 and ev >= -1.0 and risk <= 65:
        passed, reason = True, "HIGH_QUALITY"
    elif final >= 68 and ev >= 1.5 and risk <= 58:
        passed, reason = True, "POSITIVE_EV"
    else:
        passed = False
        if final < 68: reason = "FINAL_SIGNAL_LOW"
        elif risk > 65: reason = "RISK_HIGH"
        else: reason = "EV_LOW"

    if ev >= 5: ev_label = "🟢 POSITIVE"
    elif ev >= 0: ev_label = "🟡 MARGINAL"
    else: ev_label = "🔴 NEGATIVE"
    if risk <= 35: risk_label = "🟢 LOW"
    elif risk <= 58: risk_label = "🟡 MEDIUM"
    else: risk_label = "🔴 HIGH"

    return {
        "ev_estimate_percent": round(ev, 1),
        "ev_continuation_probability": round(continuation, 1),
        "ev_upside_percent": round(upside, 1),
        "ev_downside_percent": round(downside, 1),
        "ev_label": ev_label,
        "risk_score": round(risk, 1),
        "risk_label": risk_label,
        "ev_risk_passed": passed,
        "ev_risk_reason": reason,
        "ev_risk_version": "v1",
    }


def enrich_with_ev_risk(alert: dict[str, Any]) -> dict[str, Any]:
    try:
        return {**alert, **calculate_ev_risk(alert)}
    except Exception:
        return {**alert, "ev_risk_passed": False, "ev_risk_reason": "ENGINE_ERROR", "ev_risk_version": "v1-error"}
