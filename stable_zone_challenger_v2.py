"""Stable Zone Challenger v2 — pre-registered future-only shadow test.

Locked membership at EARLY entry:
  Price 20–50¢ × Early 60–69 × Side=YES × Acceleration=UP.

Shadow/Paper only. No live routing or trading logic is changed.
No pre-launch candidates are backfilled.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import math
from statistics import median

from database import get_connection

VERSION = "v2-stable-zone-challenger"
ZONE_ID = "PRICE_20_50__EARLY_60_69__YES__ACCEL_UP"
CHECKPOINTS = (180, 360, 720, 1440)

# Pre-registered before future-only results.
MIN_N_24H = 100
MIN_ROI_24H = 1.5
MIN_PF_24H = 1.40
MIN_ROLLING50_ROI = 0.0
MAX_TOP5_GROSS_PROFIT_PCT = 60.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema() -> None:
    with closing(get_connection()) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS stable_zone_challenger_v2_meta(
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stable_zone_challenger_v2_frozen(
              candidate_id INTEGER PRIMARY KEY,
              zone_id TEXT NOT NULL,
              frozen_at TEXT NOT NULL,
              side TEXT NOT NULL,
              entry_yes REAL NOT NULL,
              early_score REAL NOT NULL,
              acceleration REAL NOT NULL,
              version TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_stable_zone_challenger_v2_zone
              ON stable_zone_challenger_v2_frozen(zone_id, frozen_at);
            """
        )
        c.commit()


def ensure_launch() -> str:
    ensure_schema()
    with closing(get_connection()) as c:
        row = c.execute(
            "SELECT value FROM stable_zone_challenger_v2_meta WHERE key='launch_at'"
        ).fetchone()
        if row:
            return str(row[0])
        ts = _now()
        c.execute(
            "INSERT INTO stable_zone_challenger_v2_meta(key,value) VALUES('launch_at',?)",
            (ts,),
        )
        c.commit()
        return ts


def _matches(side: str, entry_yes: float, early_score: float, acceleration: float | None) -> bool:
    return (
        str(side).upper() == "YES"
        and 0.20 <= float(entry_yes) < 0.50
        and 60.0 <= float(early_score) < 70.0
        and acceleration is not None
        and float(acceleration) > 0.05
    )


def freeze_challenger_candidates(candidate_ids: list[int] | None = None) -> int:
    """Freeze membership only for post-launch EARLY candidates with frozen features."""
    launch = ensure_launch()
    added = 0
    with closing(get_connection()) as c:
        sql = """SELECT e.id,e.opened_at,e.side,e.entry_yes,e.early_score,f.acceleration
                 FROM entry_discovery_candidates e
                 JOIN entry_feature_snapshots f ON f.candidate_id=e.id
                 LEFT JOIN stable_zone_challenger_v2_frozen z ON z.candidate_id=e.id
                 WHERE z.candidate_id IS NULL AND e.opened_at>=?"""
        params: list[object] = [launch]
        if candidate_ids:
            ph = ",".join("?" for _ in candidate_ids)
            sql += f" AND e.id IN ({ph})"
            params.extend(int(x) for x in candidate_ids)
        sql += " ORDER BY e.id"
        for cid, opened, side, entry_yes, early_score, acceleration in c.execute(sql, tuple(params)).fetchall():
            if not _matches(side, float(entry_yes), float(early_score), acceleration):
                continue
            c.execute(
                """INSERT OR IGNORE INTO stable_zone_challenger_v2_frozen
                   (candidate_id,zone_id,frozen_at,side,entry_yes,early_score,acceleration,version)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (int(cid), ZONE_ID, str(opened), str(side), float(entry_yes),
                 float(early_score), float(acceleration), VERSION),
            )
            added += 1
        c.commit()
    return added


def _max_drawdown_dollars(vals: list[float]) -> float:
    equity = peak = max_dd = 0.0
    for roi in vals:
        equity += float(roi)  # $100 counterfactual: ROI% numerically equals dollar PnL.
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _stats(vals) -> dict:
    vals = [float(x) for x in vals]
    n = len(vals)
    if not n:
        return {"n": 0, "roi": None, "pf": None, "win": None, "median": None,
                "pnl": 0.0, "max_dd": 0.0}
    gp = sum(max(x, 0.0) for x in vals)
    gl = -sum(min(x, 0.0) for x in vals)
    return {
        "n": n,
        "roi": sum(vals) / n,
        "pf": gp / gl if gl else (float("inf") if gp else None),
        "win": 100.0 * sum(x > 0 for x in vals) / n,
        "median": median(vals),
        "pnl": sum(vals),
        "max_dd": _max_drawdown_dollars(vals),
    }


def _concentration(vals: list[float], k: int) -> float | None:
    wins = sorted((float(x) for x in vals if float(x) > 0), reverse=True)
    gross = sum(wins)
    return 100.0 * sum(wins[:k]) / gross if gross > 0 else None


def get_stable_zone_challenger_v2_report() -> dict:
    launch = ensure_launch()
    # Safe catch-up: launch_at still prevents historical backfill.
    freeze_challenger_candidates()
    with closing(get_connection()) as c:
        frozen = int(c.execute(
            "SELECT COUNT(*) FROM stable_zone_challenger_v2_frozen WHERE version=?", (VERSION,)
        ).fetchone()[0] or 0)
        stats = {}
        raw = {}
        for cp in CHECKPOINTS:
            rows = c.execute(
                """SELECT o.roi
                   FROM stable_zone_challenger_v2_frozen z
                   JOIN entry_discovery_outcomes o ON o.candidate_id=z.candidate_id
                   WHERE z.version=? AND o.checkpoint_minutes=?
                   ORDER BY z.frozen_at,z.candidate_id""",
                (VERSION, cp),
            ).fetchall()
            vals = [float(r[0]) for r in rows]
            raw[cp] = vals
            stats[cp] = _stats(vals)
        recent = c.execute(
            """SELECT e.title,z.side,z.entry_yes,z.early_score,z.acceleration
               FROM stable_zone_challenger_v2_frozen z
               JOIN entry_discovery_candidates e ON e.id=z.candidate_id
               WHERE z.version=? ORDER BY z.candidate_id DESC LIMIT 5""",
            (VERSION,),
        ).fetchall()

    vals24 = raw[1440]
    s24 = stats[1440]
    rolling50 = _stats(vals24[-50:])
    top5 = _concentration(vals24, 5)
    gate = {
        "n100": s24["n"] >= MIN_N_24H,
        "roi": s24["roi"] is not None and s24["roi"] > MIN_ROI_24H,
        "pf": s24["pf"] is not None and s24["pf"] > MIN_PF_24H,
        "rolling50": rolling50["roi"] is not None and rolling50["roi"] > MIN_ROLLING50_ROI,
        "concentration": top5 is not None and top5 < MAX_TOP5_GROSS_PROFIT_PCT,
    }
    if s24["n"] < MIN_N_24H:
        status = "🟡 VALIDATING"
    elif all(gate.values()):
        status = "🟢 PASSED"
    else:
        status = "🔴 FAILED GATE"
    return {
        "launch_at": launch, "frozen": frozen, "stats": stats, "recent": recent,
        "rolling50": rolling50, "top5": top5, "gate": gate, "status": status,
    }


def _pct(v) -> str:
    return "—" if v is None else f"{v:+.1f}%"


def _pf(v) -> str:
    if v is None:
        return "—"
    return "∞" if math.isinf(v) else f"{v:.2f}"


def format_stable_zone_challenger_v2_report(r: dict) -> str:
    lines = [
        "🚀 Stable Zone Challenger v2 · FUTURE-ONLY",
        "Locked: Price 20–50¢ × Early 60–69 × YES × Acceleration=UP",
        "Primary horizon: 24ч",
        f"Статус: {r['status']}",
        f"Frozen signals: {r['frozen']}",
        "",
        "📊 Результаты",
    ]
    labels = {180: "3ч", 360: "6ч", 720: "12ч", 1440: "24ч"}
    for cp in CHECKPOINTS:
        s = r["stats"][cp]
        win = "—" if s["win"] is None else f"{s['win']:.1f}%"
        med = "—" if s["median"] is None else f"{s['median']:+.1f}%"
        lines.append(
            f"• {labels[cp]}: n={s['n']} · ROI {_pct(s['roi'])} · PF {_pf(s['pf'])} · "
            f"Win {win} · Median {med} · MaxDD ${s['max_dd']:.1f}"
        )
    s24 = r["stats"][1440]
    r50 = r["rolling50"]
    top5 = r["top5"]
    top5s = "—" if top5 is None else f"{top5:.1f}%"
    g = r["gate"]
    lines += [
        "",
        "🧪 Pre-registered gate",
        f"• 24ч n≥100: {'✅' if g['n100'] else '⏳'} ({s24['n']})",
        f"• ROI > +1.5%: {'✅' if g['roi'] else '⏳'} ({_pct(s24['roi'])})",
        f"• PF > 1.40: {'✅' if g['pf'] else '⏳'} ({_pf(s24['pf'])})",
        f"• Rolling-50 ROI > 0: {'✅' if g['rolling50'] else '⏳'} ({_pct(r50['roi'])})",
        f"• Top-5 <60% gross profit: {'✅' if g['concentration'] else '⏳'} ({top5s})",
    ]
    if r["recent"]:
        lines += ["", "Последние frozen:"]
        for title, side, entry, early, accel in r["recent"]:
            t = str(title)
            if len(t) > 52:
                t = t[:51] + "…"
            lines.append(
                f"• {side} · {float(entry)*100:.1f}¢ · Early {float(early):.0f} · "
                f"Accel {float(accel):+.3f} · {t}"
            )
    lines += [
        "",
        "ℹ️ Чистый future-only Shadow/Paper тест. Старые кандидаты не backfill'ятся; Live/Trade не меняются.",
    ]
    return "\n".join(lines)
