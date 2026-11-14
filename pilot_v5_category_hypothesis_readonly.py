"""Read-only exploratory v5 category ambiguity hypothesis report.

Counterfactual outcome observations are NOT executable PAPER trades.
No changes to v2/v4, orders, schema, or database.
"""
import argparse
import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pilot_market_regime_audit_v2 import PATTERNS, category

EXCLUDED = frozenset(("CRYPTO", "SPORTS", "AI_TECH"))
HORIZONS = (180, 360, 720, 1440)


def utc(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def stats(values):
    if not values:
        return {"n": 0, "mean_roi_pct": None, "profit_factor": None, "win_pct": None}
    wins = sum(x > 0 for x in values)
    gp = sum(max(x, 0) for x in values)
    gl = -sum(min(x, 0) for x in values)
    return {"n": len(values), "mean_roi_pct": round(sum(values) / len(values), 3),
            "profit_factor": round(gp / gl, 3) if gl else ("infinite" if gp else None),
            "win_pct": round(100 * wins / len(values), 1)}


def analyze(db_path):
    db = Path(db_path).resolve(strict=True)
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        launch_row = conn.execute(
            "SELECT value FROM pilot_engine_v4_meta WHERE key='launch_at'").fetchone()
        if not launch_row:
            return {"status": "no_v4_launch"}
        launch = launch_row[0]
        # Same frozen source, entry bands and launch boundary as v4.
        rows = conn.execute("""
            SELECT z.candidate_id,z.frozen_at,e.opened_at,e.title,e.category,
                   d.action
            FROM stable_zone_challenger_v2_frozen z
            JOIN entry_discovery_candidates e ON e.id=z.candidate_id
            LEFT JOIN pilot_engine_v4_decisions d ON d.candidate_id=z.candidate_id
            WHERE z.version='v2-stable-zone-challenger' AND z.frozen_at>=?
              AND z.entry_yes BETWEEN 0.20 AND 0.50
              AND z.early_score BETWEEN 60 AND 69
              AND z.acceleration BETWEEN 0.68 AND 1.07
        """, (launch,)).fetchall()
        now = datetime.now(timezone.utc)
        cohorts = defaultdict(list)
        ids = []
        for cid, frozen, opened, title, raw, action in rows:
            matches = {name for name, pattern in PATTERNS
                       if re.search(pattern, str(title or ""), re.I)}
            assigned = category(title, raw)
            if assigned in EXCLUDED:
                cohort = "excluded_ambiguous" if len(matches) > 1 else "excluded_single"
            elif matches & EXCLUDED:
                cohort = "retained_conflicting_keywords"
            else:
                cohort = "retained_clean"
            cohorts[cohort].append((cid, opened, action))
            ids.append(cid)
        outcomes = defaultdict(dict)
        # Bound the query size; avoid SQLite parameter limit and any raw title export.
        for offset in range(0, len(ids), 400):
            chunk = ids[offset:offset + 400]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            for cid, h, measured, roi in conn.execute(
                "SELECT candidate_id,checkpoint_minutes,measured_at,roi "
                "FROM entry_discovery_outcomes WHERE candidate_id IN (" + placeholders +
                ") AND checkpoint_minutes IN (180,360,720,1440)", chunk):
                outcomes[cid][h] = (measured, roi)
        result = {"status": "exploratory_read_only", "launch_at": launch,
                  "total_frozen_eligible": len(rows), "cohorts": {}}
        for label, members in sorted(cohorts.items()):
            data = {"candidates": len(members),
                    "v4_trade_decisions": sum(a == "TRADE" for _, _, a in members),
                    "horizons": {}}
            for h in HORIZONS:
                due = 0
                clean = []
                missing = late = early = 0
                for cid, opened, _ in members:
                    target = utc(opened).timestamp() + h * 60
                    if target > now.timestamp():
                        continue
                    due += 1
                    item = outcomes[cid].get(h)
                    if not item or item[0] is None or item[1] is None:
                        missing += 1
                        continue
                    lag = (utc(item[0]).timestamp() - target) / 60
                    if lag < 0:
                        early += 1
                    elif lag > 15:
                        late += 1
                    else:
                        clean.append(float(item[1]))
                data["horizons"][str(h)] = {"due": due, "missing": missing,
                    "early": early, "late": late, "on_time": stats(clean)}
            result["cohorts"][label] = data
        result["limitations"] = (
            "Outcomes are observations for frozen candidates, not simulated v5 fills. "
            "Excluded candidates were never v4 trades; comparisons ignore capital occupancy, "
            "daily/open limits, fees, slippage, liquidity, and overlapping candidates. "
            "Category keyword rules are heuristic. No strategy selection from in-sample results."
        )
        conn.rollback()
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="/app/data/polymarket.db")
    args = parser.parse_args()
    print(json.dumps(analyze(args.db), ensure_ascii=False, indent=2))
