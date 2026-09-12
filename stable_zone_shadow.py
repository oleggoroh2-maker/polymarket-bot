"""Stable Zone Shadow Strategy v1.

Future-only validation of the first walk-forward stable EARLY zone:
  entry YES price 20–50¢ AND Early Score 60–69.

Shadow/Paper only. No live routing or trading logic is changed.
Membership is frozen at EARLY entry time after module launch.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import math
from statistics import median

from database import get_connection

VERSION = "v1-stable-zone-shadow"
ZONE_ID = "PRICE_20_50__EARLY_60_69"
CHECKPOINTS = (180, 360, 720, 1440)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema() -> None:
    with closing(get_connection()) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS stable_zone_shadow_meta(
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stable_zone_shadow_frozen(
              candidate_id INTEGER PRIMARY KEY,
              zone_id TEXT NOT NULL,
              frozen_at TEXT NOT NULL,
              side TEXT NOT NULL,
              entry_yes REAL NOT NULL,
              early_score REAL NOT NULL,
              version TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_stable_zone_shadow_zone
              ON stable_zone_shadow_frozen(zone_id, frozen_at);
            """
        )
        c.commit()


def ensure_launch() -> str:
    ensure_schema()
    with closing(get_connection()) as c:
        row = c.execute(
            "SELECT value FROM stable_zone_shadow_meta WHERE key='launch_at'"
        ).fetchone()
        if row:
            return str(row[0])
        ts = _now()
        c.execute(
            "INSERT INTO stable_zone_shadow_meta(key,value) VALUES('launch_at',?)",
            (ts,),
        )
        c.commit()
        return ts


def _matches(entry_yes: float, early_score: float) -> bool:
    return 0.20 <= float(entry_yes) < 0.50 and 60.0 <= float(early_score) < 70.0


def freeze_stable_zone_candidates(candidate_ids: list[int] | None = None) -> int:
    """Freeze zone membership for new EARLY candidates only.

    The module launch timestamp prevents pre-launch candidates from being backfilled
    into future-only validation.
    """
    launch = ensure_launch()
    added = 0
    with closing(get_connection()) as c:
        sql = """SELECT e.id,e.opened_at,e.side,e.entry_yes,e.early_score
                 FROM entry_discovery_candidates e
                 LEFT JOIN stable_zone_shadow_frozen z ON z.candidate_id=e.id
                 WHERE z.candidate_id IS NULL AND e.opened_at>=?"""
        params: list[object] = [launch]
        if candidate_ids:
            ph = ",".join("?" for _ in candidate_ids)
            sql += f" AND e.id IN ({ph})"
            params.extend(int(x) for x in candidate_ids)
        sql += " ORDER BY e.id"
        for cid, opened, side, entry_yes, early_score in c.execute(sql, tuple(params)).fetchall():
            if not _matches(float(entry_yes), float(early_score)):
                continue
            c.execute(
                """INSERT OR IGNORE INTO stable_zone_shadow_frozen
                   (candidate_id,zone_id,frozen_at,side,entry_yes,early_score,version)
                   VALUES(?,?,?,?,?,?,?)""",
                (int(cid), ZONE_ID, str(opened), str(side), float(entry_yes), float(early_score), VERSION),
            )
            added += 1
        c.commit()
    return added


def _max_drawdown_dollars(rois: list[float]) -> float:
    """Max peak-to-trough drawdown of a sequential $100-per-trade PnL curve."""
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for roi in rois:
        equity += float(roi)  # $100 stake => ROI% numerically equals dollar PnL
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _stats(rows) -> dict:
    vals = [float(r[1]) for r in rows]
    n = len(vals)
    if not n:
        return {
            "n": 0, "roi": None, "pf": None, "win": None, "median": None,
            "pnl": 0.0, "max_dd": 0.0, "last20_roi": None,
        }
    gp = sum(max(v, 0.0) for v in vals)
    gl = -sum(min(v, 0.0) for v in vals)
    return {
        "n": n,
        "roi": sum(vals) / n,
        "pf": gp / gl if gl else (float("inf") if gp else None),
        "win": sum(v > 0 for v in vals) / n * 100.0,
        "median": median(vals),
        "pnl": sum(vals),
        "max_dd": _max_drawdown_dollars(vals),
        "last20_roi": (sum(vals[-20:]) / len(vals[-20:])) if vals else None,
    }


def get_stable_zone_shadow_report() -> dict:
    launch = ensure_launch()
    # Catch candidates recorded since launch in case a transient integration call failed.
    freeze_stable_zone_candidates()
    with closing(get_connection()) as c:
        frozen = int(c.execute(
            "SELECT COUNT(*) FROM stable_zone_shadow_frozen WHERE version=?",
            (VERSION,),
        ).fetchone()[0] or 0)
        stats = {}
        for cp in CHECKPOINTS:
            rows = c.execute(
                """SELECT o.measured_at,o.roi
                   FROM stable_zone_shadow_frozen z
                   JOIN entry_discovery_outcomes o ON o.candidate_id=z.candidate_id
                   WHERE z.version=? AND o.checkpoint_minutes=?
                   ORDER BY z.frozen_at,z.candidate_id""",
                (VERSION, cp),
            ).fetchall()
            stats[cp] = _stats(rows)
        recent = c.execute(
            """SELECT e.title,z.side,z.entry_yes,z.early_score,z.frozen_at
               FROM stable_zone_shadow_frozen z
               JOIN entry_discovery_candidates e ON e.id=z.candidate_id
               WHERE z.version=? ORDER BY z.candidate_id DESC LIMIT 5""",
            (VERSION,),
        ).fetchall()

    s24 = stats[1440]
    # Pre-registered promotion rule from the research plan.
    if s24["n"] >= 40 and (s24["roi"] or 0) > 0 and (s24["pf"] or 0) > 1.2 and (s24["last20_roi"] or 0) > -1.0:
        status = "🟢 CANDIDATE"
    elif s24["n"] >= 20 and ((s24["roi"] or 0) <= 0 or (s24["pf"] or 0) < 1.0):
        status = "🔴 WEAKENING"
    else:
        status = "🟡 VALIDATING"
    return {
        "version": VERSION,
        "launch_at": launch,
        "frozen": frozen,
        "stats": stats,
        "recent": recent,
        "status": status,
    }


def _pct(v) -> str:
    return "—" if v is None else f"{v:+.1f}%"


def _pf(v) -> str:
    if v is None:
        return "—"
    if math.isinf(v):
        return "∞"
    return f"{v:.2f}"


def format_stable_zone_shadow_report(r: dict) -> str:
    lines = [
        "🎯 Stable Zone Shadow v1 · FUTURE-ONLY",
        "Zone: Price 20–50¢ × Early 60–69",
        "Основной горизонт: 24ч",
        f"Статус: {r['status']}",
        f"Frozen signals: {r['frozen']}",
        "",
        "📊 Результаты",
    ]
    labels = {180: "3ч", 360: "6ч", 720: "12ч", 1440: "24ч"}
    for cp in CHECKPOINTS:
        s = r["stats"][cp]
        med = "—" if s["median"] is None else f"{s['median']:+.1f}%"
        win = "—" if s["win"] is None else f"{s['win']:.1f}%"
        lines.append(
            f"• {labels[cp]}: n={s['n']} · ROI {_pct(s['roi'])} · PF {_pf(s['pf'])} · "
            f"Win {win} · Median {med} · MaxDD ${s['max_dd']:.1f}"
        )
    s24 = r["stats"][1440]
    lines += [
        "",
        "🧪 Pre-registered gate для CANDIDATE",
        f"• n≥40: {'✅' if s24['n'] >= 40 else '⏳'} ({s24['n']})",
        f"• ROI>0: {'✅' if s24['roi'] is not None and s24['roi'] > 0 else '⏳'} ({_pct(s24['roi'])})",
        f"• PF>1.20: {'✅' if s24['pf'] is not None and s24['pf'] > 1.2 else '⏳'} ({_pf(s24['pf'])})",
        f"• Последние 20 ROI > -1%: {'✅' if s24['last20_roi'] is not None and s24['last20_roi'] > -1.0 else '⏳'} ({_pct(s24['last20_roi'])})",
    ]
    if r["recent"]:
        lines += ["", "Последние frozen:"]
        for title, side, entry, early, _ts in r["recent"]:
            t = str(title)
            if len(t) > 58:
                t = t[:57] + "…"
            lines.append(f"• {side} · {float(entry)*100:.1f}¢ · Early {float(early):.0f} · {t}")
    lines += [
        "",
        "ℹ️ Только Shadow/Paper. Membership фиксируется при EARLY-входе; live routing, Entry v1/v2 и Trade v2/v3 не меняются.",
    ]
    return "\n".join(lines)
