"""Offline, read-only timing-quality audit for pilot v2/v4 PAPER decisions."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from paper_checkpoint_quality import audit_checkpoint_rows

ENGINES = {"v2": ("pilot_engine_v2_decisions", "v2-pilot-engine"),
           "v4": ("pilot_engine_v4_decisions", "v4-category-filter-future-only")}


def audit_pilot_database(db_path, now=None):
    """Audit offline SQLite backup only; no decision processing or writes."""
    path = Path(db_path).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        report = {}
        for engine, (table, version) in ENGINES.items():
            candidates = conn.execute(
                "SELECT e.id, e.opened_at FROM " + table + " d "
                "JOIN entry_discovery_candidates e ON e.id=d.candidate_id "
                "WHERE d.action='TRADE' AND d.version=?"
            , (version,)).fetchall()
            outcomes = conn.execute(
                "SELECT o.candidate_id,o.checkpoint_minutes,o.measured_at "
                "FROM entry_discovery_outcomes o JOIN " + table + " d "
                "ON d.candidate_id=o.candidate_id WHERE d.action='TRADE' AND d.version=?"
            , (version,)).fetchall()
            report[engine] = {
                "trades": len(candidates),
                "checkpoint_quality": audit_checkpoint_rows(candidates, outcomes, now=now)
            }
    return report


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("database", help="Offline SQLite backup (not live Railway DB)")
    args = p.parse_args()
    print(json.dumps(audit_pilot_database(args.database), indent=2))
