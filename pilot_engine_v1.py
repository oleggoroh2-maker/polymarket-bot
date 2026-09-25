"""Pilot Engine v1 — controlled PAPER pilot for the passed Stable Zone Challenger v2.

Entry rule is intentionally identical to the pre-registered challenger:
Price 20–50c × Early 60–69 × YES × Acceleration > 0.05.

This module DOES NOT place real orders. It creates future-only paper pilot decisions
with production-style risk limits so execution/risk behavior can be validated before
any exchange executor is connected.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone, timedelta
import math
from statistics import median

import config
from database import get_connection

VERSION = "v1-pilot-engine"
HORIZON_MINUTES = 1440


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _cfg(name: str, default):
    return getattr(config, name, default)


def ensure_schema() -> None:
    with closing(get_connection()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS pilot_engine_v1_meta(
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pilot_engine_v1_decisions(
          candidate_id INTEGER PRIMARY KEY,
          decided_at TEXT NOT NULL,
          action TEXT NOT NULL,
          reason TEXT NOT NULL,
          stake REAL NOT NULL,
          entry_yes REAL NOT NULL,
          early_score REAL NOT NULL,
          acceleration REAL NOT NULL,
          version TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pilot_v1_decided
          ON pilot_engine_v1_decisions(decided_at, action);
        """)
        c.commit()


def ensure_launch() -> str:
    ensure_schema()
    with closing(get_connection()) as c:
        row = c.execute("SELECT value FROM pilot_engine_v1_meta WHERE key='launch_at'").fetchone()
        if row:
            return str(row[0])
        ts = _iso(_now())
        c.execute("INSERT INTO pilot_engine_v1_meta(key,value) VALUES('launch_at',?)", (ts,))
        c.commit()
        return ts


def _realized_rows(c):
    return c.execute("""
      SELECT d.decided_at,d.stake,o.roi
      FROM pilot_engine_v1_decisions d
      JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
      WHERE d.version=? AND d.action='TRADE' AND o.checkpoint_minutes=?
      ORDER BY d.decided_at,d.candidate_id
    """, (VERSION, HORIZON_MINUTES)).fetchall()


def _realized_drawdown(c) -> float:
    equity = peak = max_dd = 0.0
    for _ts, stake, roi in _realized_rows(c):
        equity += float(stake) * float(roi) / 100.0
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _risk_reason(c, decided_at: datetime) -> str | None:
    if _realized_drawdown(c) >= float(_cfg("PILOT_MAX_DRAWDOWN_USD", 100.0)):
        return "KILL_SWITCH_DRAWDOWN"

    day_start = decided_at.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    daily = int(c.execute("""
      SELECT COUNT(*) FROM pilot_engine_v1_decisions
      WHERE version=? AND action='TRADE' AND decided_at>=? AND decided_at<=?
    """, (VERSION, day_start, decided_at.isoformat())).fetchone()[0] or 0)
    if daily >= int(_cfg("PILOT_MAX_NEW_TRADES_PER_DAY", 8)):
        return "DAILY_LIMIT"

    open_cutoff = (decided_at - timedelta(minutes=HORIZON_MINUTES)).isoformat()
    open_count = int(c.execute("""
      SELECT COUNT(*) FROM pilot_engine_v1_decisions d
      LEFT JOIN entry_discovery_outcomes o
        ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=?
      WHERE d.version=? AND d.action='TRADE' AND d.decided_at>=? AND d.decided_at<=? AND o.candidate_id IS NULL
    """, (HORIZON_MINUTES, VERSION, open_cutoff, decided_at.isoformat())).fetchone()[0] or 0)
    if open_count >= int(_cfg("PILOT_MAX_OPEN_POSITIONS", 5)):
        return "MAX_OPEN_POSITIONS"
    return None


def process_pilot_candidates(candidate_ids: list[int] | None = None) -> int:
    """Freeze post-launch pilot TRADE/SKIP decisions. Never backfills pre-launch rows."""
    launch = ensure_launch()
    added = 0
    with closing(get_connection()) as c:
        sql = """
          SELECT z.candidate_id,z.frozen_at,z.entry_yes,z.early_score,z.acceleration
          FROM stable_zone_challenger_v2_frozen z
          LEFT JOIN pilot_engine_v1_decisions p ON p.candidate_id=z.candidate_id
          WHERE z.version='v2-stable-zone-challenger' AND z.frozen_at>=? AND p.candidate_id IS NULL
        """
        params: list[object] = [launch]
        if candidate_ids:
            ph = ",".join("?" for _ in candidate_ids)
            sql += f" AND z.candidate_id IN ({ph})"
            params.extend(int(x) for x in candidate_ids)
        sql += " ORDER BY z.frozen_at,z.candidate_id"
        for cid, frozen_at, price, early, accel in c.execute(sql, tuple(params)).fetchall():
            try:
                ts = datetime.fromisoformat(str(frozen_at))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                ts = _now()
            reason = _risk_reason(c, ts)
            action = "SKIP" if reason else "TRADE"
            reason = reason or "ELIGIBLE"
            stake = float(_cfg("PILOT_STAKE_USD", 20.0)) if action == "TRADE" else 0.0
            c.execute("""
              INSERT OR IGNORE INTO pilot_engine_v1_decisions
              (candidate_id,decided_at,action,reason,stake,entry_yes,early_score,acceleration,version)
              VALUES(?,?,?,?,?,?,?,?,?)
            """, (int(cid), str(frozen_at), action, reason, stake, float(price), float(early), float(accel), VERSION))
            added += 1
        c.commit()
    return added


def _stats(rows) -> dict:
    vals = [(float(stake), float(roi)) for stake, roi in rows]
    if not vals:
        return {"n": 0, "roi": None, "pf": None, "win": None, "median": None, "pnl": 0.0, "max_dd": 0.0}
    rois = [r for _, r in vals]
    pnls = [s * r / 100.0 for s, r in vals]
    gp = sum(max(x, 0.0) for x in pnls)
    gl = -sum(min(x, 0.0) for x in pnls)
    eq = peak = dd = 0.0
    for x in pnls:
        eq += x; peak = max(peak, eq); dd = max(dd, peak - eq)
    return {
        "n": len(vals), "roi": sum(rois) / len(rois),
        "pf": gp / gl if gl else (float("inf") if gp else None),
        "win": 100.0 * sum(r > 0 for r in rois) / len(rois),
        "median": median(rois), "pnl": sum(pnls), "max_dd": dd,
    }


def get_pilot_engine_v1_report() -> dict:
    launch = ensure_launch()
    process_pilot_candidates()  # safe catch-up: launch_at prevents historical backfill
    with closing(get_connection()) as c:
        counts = dict(c.execute("SELECT action,COUNT(*) FROM pilot_engine_v1_decisions WHERE version=? GROUP BY action", (VERSION,)).fetchall())
        reasons = c.execute("""
          SELECT reason,COUNT(*) FROM pilot_engine_v1_decisions
          WHERE version=? AND action='SKIP' GROUP BY reason ORDER BY COUNT(*) DESC
        """, (VERSION,)).fetchall()
        # Dashboard analytics are read-only: expose the same 3h/6h/12h/24h
        # outcome shape as Pilot v2 without changing Pilot v1 decision logic.
        horizons = {}
        for checkpoint in (180, 360, 720, 1440):
            checkpoint_rows = c.execute("""
              SELECT d.stake,o.roi FROM pilot_engine_v1_decisions d
              JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
              WHERE d.version=? AND d.action='TRADE' AND o.checkpoint_minutes=?
              ORDER BY d.decided_at,d.candidate_id
            """, (VERSION, checkpoint)).fetchall()
            horizons[checkpoint] = _stats(checkpoint_rows)
        rows = c.execute("""
          SELECT d.stake,o.roi FROM pilot_engine_v1_decisions d
          JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
          WHERE d.version=? AND d.action='TRADE' AND o.checkpoint_minutes=?
          ORDER BY d.decided_at,d.candidate_id
        """, (VERSION, HORIZON_MINUTES)).fetchall()
        now_iso = _iso(_now())
        open_cutoff = _iso(_now() - timedelta(minutes=HORIZON_MINUTES))
        open_n = int(c.execute("""
          SELECT COUNT(*) FROM pilot_engine_v1_decisions d
          LEFT JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=?
          WHERE d.version=? AND d.action='TRADE' AND d.decided_at>=? AND d.decided_at<=? AND o.candidate_id IS NULL
        """, (HORIZON_MINUTES, VERSION, open_cutoff, now_iso)).fetchone()[0] or 0)
        recent = c.execute("""
          SELECT e.title,d.action,d.reason,d.entry_yes,d.acceleration,d.stake
          FROM pilot_engine_v1_decisions d JOIN entry_discovery_candidates e ON e.id=d.candidate_id
          WHERE d.version=? ORDER BY d.candidate_id DESC LIMIT 5
        """, (VERSION,)).fetchall()
    s = _stats(rows)
    kill = s["max_dd"] >= float(_cfg("PILOT_MAX_DRAWDOWN_USD", 100.0))
    return {"launch": launch, "counts": counts, "reasons": reasons, "stats": s,
            "horizons": horizons, "open": open_n, "recent": recent, "kill": kill}


def _pct(v): return "—" if v is None else f"{v:+.1f}%"
def _pf(v):
    if v is None: return "—"
    return "∞" if math.isinf(v) else f"{v:.2f}"


def format_pilot_engine_v1_report(r: dict) -> str:
    s = r["stats"]
    lines = [
        "🧪 Pilot Engine v1 · PAPER",
        "Rule: Price 20–50¢ × Early 60–69 × YES × Acceleration>0.05",
        f"Stake: ${float(_cfg('PILOT_STAKE_USD',20.0)):.0f} · Max open: {int(_cfg('PILOT_MAX_OPEN_POSITIONS',5))} · Daily: {int(_cfg('PILOT_MAX_NEW_TRADES_PER_DAY',8))}",
        f"Kill-switch DD: ${float(_cfg('PILOT_MAX_DRAWDOWN_USD',100.0)):.0f} · {'🔴 TRIGGERED' if r['kill'] else '🟢 OK'}",
        "",
        f"Decisions: TRADE {int(r['counts'].get('TRADE',0))} · SKIP {int(r['counts'].get('SKIP',0))} · Open {r['open']}",
        f"24ч matured: n={s['n']} · ROI {_pct(s['roi'])} · PF {_pf(s['pf'])} · Win {_pct(s['win'])}",
        f"Median {_pct(s['median'])} · PnL ${s['pnl']:+.2f} · MaxDD ${s['max_dd']:.2f}",
    ]
    if r["reasons"]:
        lines += ["", "🛑 SKIP причины"]
        for reason, n in r["reasons"][:5]: lines.append(f"• {reason}: {n}")
    if r["recent"]:
        lines += ["", "Последние решения:"]
        for title, action, reason, price, _accel, stake in r["recent"]:
            t = str(title); t = t if len(t) <= 52 else t[:51] + "…"
            lines.append(f"• {action} · {float(price)*100:.1f}¢ · ${float(stake):.0f} · {reason} · {t}")
    lines += ["", "ℹ️ Реальные ордера НЕ отправляются. Это future-only PAPER pilot с production-style risk limits; Live/Trade не меняются."]
    return "\n".join(lines)
