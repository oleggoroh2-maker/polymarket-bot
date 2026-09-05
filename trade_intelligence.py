"""Trade Intelligence v2 — paper-only entry gate, sizing and exit plan.

No Telegram filtering and no real orders. The decision is frozen at signal time,
so Paper Trading can compare v2 against the older fixed/risk strategies without
using future outcomes.
"""
from __future__ import annotations
import math
from typing import Any
from market_regime_engine import classify_market_regime


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _pct(v: Any) -> float | None:
    if v is None: return None
    x = _num(v, float("nan"))
    if not math.isfinite(x): return None
    return x * 100.0 if 0 <= x <= 1 else x


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _is_dip(alert_type: str) -> bool:
    x = str(alert_type or "").upper()
    return any(k in x for k in ("DIP", "DROP", "BEAR"))


def calculate_trade_intelligence(alert: dict[str, Any]) -> dict[str, Any]:
    final = _clamp(_num(alert.get("final_signal_score"), 50))
    ev = _num(alert.get("ev_estimate_percent"))
    base_risk = _clamp(_num(alert.get("risk_score"), 50))
    ai_risk = _clamp(_num(alert.get("ai_risk"), 50))
    sim = _pct(alert.get("similarity_score"))
    if sim is None: sim = _pct(alert.get("similarity"))
    ml = _pct(alert.get("ml_probability"))
    liq = max(0, _num(alert.get("liquidity")))
    price = _num(alert.get("current_price", alert.get("price")))
    move = abs(_num(alert.get("change_percent")))
    vol = abs(_num(alert.get("volume_change_percent")))
    liq_ch = _num(alert.get("liquidity_change_percent"))
    spread = _num(alert.get("spread"))
    spread_pct = (spread / price * 100) if spread > 0 and price > 0 else 0
    regime = classify_market_regime(alert)
    side = "NO" if _is_dip(str(alert.get("alert_type") or "")) else "YES"
    side_price = (1.0 - price) if side == "NO" and 0 < price < 1 else price

    chase = move * 0.90
    if move >= 35: chase += 10
    if move >= 70: chase += 15
    if vol >= 80: chase -= 12
    elif vol >= 30: chase -= 5
    elif move >= 25: chase += 8
    if liq_ch >= 20: chase -= 5
    if spread_pct >= 5: chase += 10
    if regime["regime"] == "EVENT_SHOCK": chase += 20
    elif regime["regime"] == "CHAOS_MANIPULATION": chase += 5
    elif regime["regime"] == "MOMENTUM": chase -= 5
    chase = _clamp(chase)

    q = 48.0 + (final - 65) * 0.60 + ev * 1.35 - (base_risk - 45) * 0.32 - (ai_risk - 45) * 0.12
    if sim is not None: q += (sim - 70) * 0.13
    if ml is not None: q += (ml - 25) * 0.06
    if liq >= 1_000_000: q += 5
    elif liq >= 250_000: q += 3
    elif liq < 10_000: q -= 10
    # Score the actually purchased side, not only the YES quote.
    if 0.05 <= side_price <= 0.80: q += 4
    elif side_price < 0.02 or side_price > 0.97: q -= 10
    elif side_price > 0.90: q -= 5
    if spread_pct >= 8: q -= 10
    elif spread_pct >= 4: q -= 5
    q -= max(0, chase - 30) * 0.34
    if regime["regime"] == "MOMENTUM": q += 2
    elif regime["regime"] == "EVENT_SHOCK": q -= 16
    elif regime["regime"] == "CHAOS_MANIPULATION": q -= 1
    # News/Social v1 is contextual and conservative: only strong contradictions
    # or confirmed catalysts move entry quality materially. It remains Paper/Shadow.
    news_status = str(alert.get("news_status") or "").upper()
    news_score = _num(alert.get("news_score"))
    if news_status == "CONTRADICTED": q -= 18
    elif news_status == "CONFIRMED_NEWS" and news_score >= 35: q += 5
    elif news_status == "RUMOR": q -= 4
    elif news_status == "NO_CATALYST" and regime["regime"] == "CHAOS_MANIPULATION": q -= 3
    q = _clamp(q)

    reasons: list[str] = []
    decision = "TRADE"
    # v2 gate: deliberately conservative, but no live Telegram signal is blocked.
    if regime["regime"] == "EVENT_SHOCK" and not (q >= 82 and ev >= 4 and chase <= 45):
        decision = "SKIP"; reasons.append("EVENT_SHOCK")
    if chase >= 82:
        decision = "SKIP"; reasons.append("CHASE_RISK")
    if q < 48:
        decision = "SKIP"; reasons.append("LOW_ENTRY_QUALITY")
    if base_risk >= 82 and q < 78:
        decision = "SKIP"; reasons.append("HIGH_RISK")
    if side_price > 0.97:
        decision = "SKIP"; reasons.append("POOR_PAYOFF_PRICE")
    if news_status == "CONTRADICTED" and news_score >= 30:
        decision = "SKIP"; reasons.append("NEWS_CONTRADICTED")

    if q >= 84 and chase <= 40 and base_risk <= 55: stake = 150.0
    elif q >= 74 and chase <= 55: stake = 100.0
    elif q >= 64: stake = 75.0
    elif q >= 55: stake = 50.0
    else: stake = 25.0
    if regime["regime"] == "EVENT_SHOCK": stake = min(stake, 25.0)
    if chase >= 65: stake = min(stake, 25.0)
    elif chase >= 50: stake = min(stake, 50.0)
    if base_risk >= 70: stake = min(stake, 25.0)
    if decision == "SKIP": stake = 0.0

    # Exit horizon is selected only from information known at entry.
    # News/chase conditions get shorter holding periods; clean momentum can breathe.
    if regime["regime"] == "EVENT_SHOCK" or chase >= 65:
        exit_minutes = 60
    elif chase >= 45 or base_risk >= 65:
        exit_minutes = 180
    elif regime["regime"] == "MOMENTUM" and q >= 70 and chase < 35:
        exit_minutes = 720
    else:
        exit_minutes = 360

    return {
        "entry_quality_score": round(q, 1), "chase_risk_score": round(chase, 1),
        "suggested_stake_usd": stake, "trade_regime": regime["regime"],
        "trade_regime_confidence": regime["confidence"], "trade_intelligence_version": "v2",
        "trade_v2_decision": decision, "trade_v2_skip_reasons": reasons,
        "trade_v2_exit_minutes": exit_minutes, "trade_side_price": round(side_price, 6),
    }


def enrich_with_trade_intelligence(alert: dict[str, Any]) -> dict[str, Any]:
    try:
        return {**alert, **calculate_trade_intelligence(alert)}
    except Exception:
        return {**alert, "entry_quality_score": 50.0, "chase_risk_score": 50.0,
                "suggested_stake_usd": 0.0, "trade_v2_decision": "SKIP",
                "trade_v2_skip_reasons": ["ENGINE_ERROR"], "trade_v2_exit_minutes": 360,
                "trade_intelligence_version": "v2-error"}
