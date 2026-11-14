"""Read-only, aggregate-only PAPER v2/v4 diagnostics for a live SQLite WAL database.

No schema changes, orders, secrets, candidate titles, or raw trades are exported.
Run manually inside the worker container; does not run on import.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

VERSIONS = {
    "v2": ("pilot_engine_v2_decisions", "v2-pilot-engine"),
    "v4": ("pilot_engine_v4_decisions", "v4-category-filter-future-only"),
}
HORIZONS = (180, 360, 720, 1440)


def utc(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def analyze(path):
    db = Path(path).resolve(strict=True)
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("BEGIN")  # consistent snapshot for all SELECTs; no writes
        now = datetime.now(timezone.utc)
        result = {"generated_at_utc": now.isoformat(), "strategies": {}}
        for name, (table, version) in VERSIONS.items():
            counts = dict(conn.execute(
                "SELECT action,COUNT(*) FROM " + table + " WHERE version=? GROUP BY action",
                (version,),
            ).fetchall())
            rows = conn.execute(
                "SELECT e.opened_at,o.checkpoint_minutes,o.measured_at,o.roi,d.stake "
                "FROM " + table + " d "
                "JOIN entry_discovery_candidates e ON e.id=d.candidate_id "
                "LEFT JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id "
                "WHERE d.version=? AND d.action='TRADE'",
                (version,),
            ).fetchall()
            trades = conn.execute(
                "SELECT COUNT(*) FROM " + table + " WHERE version=? AND action='TRADE'",
                (version,),
            ).fetchone()[0]
            result["strategies"][name] = {"decisions": counts, "trades": trades, "horizons": {}}
            for horizon in HORIZONS:
                # row-level candidate identifiers deliberately not included in report
                # Each TRADE has one row per outcome, or a single null row if none.
                eligible = conn.execute(
                    "SELECT COUNT(*) FROM " + table + " d "
                    "JOIN entry_discovery_candidates e ON e.id=d.candidate_id "
                    "WHERE d.version=? AND d.action='TRADE' AND "
                    "julianday(e.opened_at) <= julianday(?) - ? / 1440.0",
                    (version, now.isoformat(), horizon),
                ).fetchone()[0]
                delays = []
                early = late = on_time = 0
                gains = losses = pnl = 0.0
                matured = wins = 0
                clean_gains = clean_losses = clean_pnl = clean_roi = 0.0
                clean_n = clean_wins = 0
                for opened, cp, measured, roi, stake in rows:
                    if cp != horizon or measured is None:
                        continue
                    target = utc(opened).timestamp() + horizon * 60
                    if target > now.timestamp():
                        continue
                    lag = (utc(measured).timestamp() - target) / 60
                    delays.append(lag)
                    if lag < 0:
                        early += 1
                    elif lag > 15:
                        late += 1
                    else:
                        on_time += 1
                        clean_n += 1
                        clean_roi += float(roi)
                        clean_profit = float(stake) * float(roi) / 100
                        clean_pnl += clean_profit
                        clean_gains += max(clean_profit, 0)
                        clean_losses += max(-clean_profit, 0)
                        clean_wins += clean_profit > 0
                    profit = float(stake) * float(roi) / 100
                    pnl += profit
                    gains += max(profit, 0)
                    losses += max(-profit, 0)
                    matured += 1
                    wins += profit > 0
                result["strategies"][name]["horizons"][str(horizon)] = {
                    "due": eligible, "recorded": matured,
                    "missing": max(eligible - matured, 0),
                    "early": early, "on_time_0_to_15m": on_time,
                    "late_over_15m": late,
                    "max_delay_minutes": round(max(delays), 1) if delays else None,
                    "pnl_usd": round(pnl, 2),  # all recorded, including late: diagnostic only
                    "on_time_only": {
                        "n": clean_n,
                        "pnl_usd": round(clean_pnl, 2),
                        "mean_roi_pct": round(clean_roi / clean_n, 3) if clean_n else None,
                        "win_rate_pct": round(100 * clean_wins / clean_n, 1) if clean_n else None,
                        "profit_factor": round(clean_gains / clean_losses, 3) if clean_losses else ("infinite" if clean_gains else None),
                    },
                    "win_rate_pct": round(100 * wins / matured, 1) if matured else None,
                    "profit_factor": round(gains / losses, 3) if losses else ("infinite" if gains else None),
                }
        conn.rollback()
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/app/data/polymarket.db")
    args = parser.parse_args()
    print(json.dumps(analyze(args.db), ensure_ascii=False, indent=2))
