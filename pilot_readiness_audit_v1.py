"""Pilot Readiness Audit v1.

Read-only audit of the already frozen Stable Zone Challenger v2 cohort.
This module does not alter candidate membership, routing, thresholds or trading.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import math
from statistics import median

from database import get_connection
from stable_zone_challenger_v2 import VERSION as CHALLENGER_VERSION, freeze_challenger_candidates

VERSION = "v1-pilot-readiness-audit"


def _stats(vals: list[float]) -> dict:
    vals = [float(v) for v in vals]
    if not vals:
        return {"n": 0, "roi": None, "pf": None, "win": None, "median": None,
                "avg_win": None, "avg_loss": None, "max_dd": 0.0}
    wins = [v for v in vals if v > 0]
    losses = [v for v in vals if v < 0]
    gp = sum(wins)
    gl = -sum(losses)
    equity = peak = max_dd = 0.0
    for v in vals:
        equity += v
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "n": len(vals),
        "roi": sum(vals) / len(vals),
        "pf": gp / gl if gl else (float("inf") if gp else None),
        "win": 100.0 * len(wins) / len(vals),
        "median": median(vals),
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "max_dd": max_dd,
    }


def _longest_loss_streak(vals: list[float]) -> int:
    best = cur = 0
    for v in vals:
        if float(v) <= 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _bucket_stats(rows, key_fn) -> list[tuple[str, dict]]:
    groups = defaultdict(list)
    for row in rows:
        groups[key_fn(row)].append(float(row["roi24"]))
    out = [(str(k), _stats(v)) for k, v in groups.items()]
    out.sort(key=lambda x: x[1]["n"], reverse=True)
    return out


def get_pilot_readiness_audit_v1_report() -> dict:
    # Catch-up is safe: Challenger itself enforces its original future-only launch boundary.
    freeze_challenger_candidates()
    with closing(get_connection()) as c:
        c.row_factory = __import__("sqlite3").Row
        rows = c.execute(
            """SELECT z.candidate_id,z.frozen_at,e.title,z.entry_yes,z.early_score,z.acceleration,
                      COALESCE(f.category_v2,e.category,'OTHER') AS category_v2,
                      o24.roi AS roi24,o12.roi AS roi12
               FROM stable_zone_challenger_v2_frozen z
               JOIN entry_discovery_candidates e ON e.id=z.candidate_id
               JOIN entry_discovery_outcomes o24
                 ON o24.candidate_id=z.candidate_id AND o24.checkpoint_minutes=1440
               LEFT JOIN entry_discovery_outcomes o12
                 ON o12.candidate_id=z.candidate_id AND o12.checkpoint_minutes=720
               LEFT JOIN entry_feature_snapshots f ON f.candidate_id=z.candidate_id
               WHERE z.version=?
               ORDER BY z.frozen_at,z.candidate_id""",
            (CHALLENGER_VERSION,),
        ).fetchall()

    vals = [float(r["roi24"]) for r in rows]
    overall = _stats(vals)
    top = sorted(rows, key=lambda r: float(r["roi24"]), reverse=True)[:5]
    bottom = sorted(rows, key=lambda r: float(r["roi24"]))[:5]

    def price_bucket(r):
        p = float(r["entry_yes"])
        return "20–30¢" if p < .30 else ("30–40¢" if p < .40 else "40–50¢")

    def early_bucket(r):
        return "60–64" if float(r["early_score"]) < 65 else "65–69"

    def accel_bucket(r):
        a = float(r["acceleration"])
        return "0.05–0.30" if a < .30 else ("0.30–0.75" if a < .75 else "0.75+")

    paired = [r for r in rows if r["roi12"] is not None]
    deltas = [float(r["roi24"]) - float(r["roi12"]) for r in paired]
    improved = sum(d > 0 for d in deltas)
    return {
        "version": VERSION,
        "overall": overall,
        "loss_streak": _longest_loss_streak(vals),
        "categories": _bucket_stats(rows, lambda r: str(r["category_v2"] or "OTHER")),
        "prices": _bucket_stats(rows, price_bucket),
        "early": _bucket_stats(rows, early_bucket),
        "acceleration": _bucket_stats(rows, accel_bucket),
        "paired_n": len(paired),
        "delta_12_24": sum(deltas) / len(deltas) if deltas else None,
        "improved_12_24": 100.0 * improved / len(deltas) if deltas else None,
        "top": [(str(r["title"]), float(r["roi24"])) for r in top],
        "bottom": [(str(r["title"]), float(r["roi24"])) for r in bottom],
    }


def _pct(v):
    return "—" if v is None else f"{v:+.1f}%"


def _pf(v):
    if v is None:
        return "—"
    return "∞" if math.isinf(v) else f"{v:.2f}"


def _line(label: str, s: dict) -> str:
    return f"• {label}: n={s['n']} · ROI {_pct(s['roi'])} · PF {_pf(s['pf'])} · Win {_pct(s['win'])}"


def _short(s: str, n: int = 52) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def format_pilot_readiness_audit_v1_report(r: dict) -> str:
    s = r["overall"]
    improved_text = "—" if r["improved_12_24"] is None else f"{r['improved_12_24']:.1f}%"
    lines = [
        "🧾 Pilot Readiness Audit v1 · READ-ONLY",
        "Cohort: Stable Zone Challenger v2 · matured 24ч only",
        "",
        "📊 Общий профиль",
        f"• n={s['n']} · ROI {_pct(s['roi'])} · PF {_pf(s['pf'])} · Win {_pct(s['win'])} · Median {_pct(s['median'])}",
        f"• Avg win {_pct(s['avg_win'])} · Avg loss {_pct(s['avg_loss'])}",
        f"• MaxDD ${s['max_dd']:.1f} на $100/signal · Max loss streak {r['loss_streak']}",
        "",
        "⏱ 12ч → 24ч",
        f"• paired n={r['paired_n']} · среднее изменение {_pct(r['delta_12_24'])}",
        f"• улучшились к 24ч: {improved_text}",
        "",
        "🏷 Категории",
    ]
    lines += [_line(k, v) for k, v in r["categories"]]
    lines += ["", "💵 Entry price"] + [_line(k, v) for k, v in r["prices"]]
    lines += ["", "🎯 Early score"] + [_line(k, v) for k, v in r["early"]]
    lines += ["", "⚡ Acceleration"] + [_line(k, v) for k, v in r["acceleration"]]
    lines += ["", "🏆 Top-5 24ч"]
    lines += [f"• {_pct(roi)} · {_short(title)}" for title, roi in r["top"]]
    lines += ["", "🧯 Bottom-5 24ч"]
    lines += [f"• {_pct(roi)} · {_short(title)}" for title, roi in r["bottom"]]
    lines += ["", "ℹ️ Диагностика прошедшей future-only когорты. Никакие фильтры, Live/Trade или правила Challenger не изменяются."]
    return "\n".join(lines)
