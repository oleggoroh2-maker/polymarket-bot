"""Read-only quality audit for PAPER checkpoint timestamps.

This module does not alter historical outcomes or trading decisions.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

CHECKPOINTS = (180, 360, 720, 1440)


def _datetime(value):
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def audit_checkpoint_rows(candidates, outcomes, now=None, checkpoints=CHECKPOINTS):
    """Return timing quality by horizon, without interpreting ROI as executable PnL.

    candidates: iterable of (candidate_id, opened_at).
    outcomes: iterable of (candidate_id, checkpoint_minutes, measured_at).
    """
    now = _datetime(now) if now is not None else datetime.now(timezone.utc)
    measured = {(int(cid), int(cp)): _datetime(ts) for cid, cp, ts in outcomes}
    result = {}
    for cp in checkpoints:
        stats = dict(eligible=0, recorded=0, missing=0, on_time=0,
                     delayed=0, delay_minutes_max=None, delay_minutes_avg=None)
        delays = []
        for cid, opened_at in candidates:
            target = _datetime(opened_at).timestamp() + int(cp) * 60
            if target > now.timestamp():
                continue
            stats["eligible"] += 1
            stamp = measured.get((int(cid), int(cp)))
            if stamp is None:
                stats["missing"] += 1
                continue
            stats["recorded"] += 1
            lag = (stamp.timestamp() - target) / 60
            delays.append(lag)
            if lag > 0:
                stats["delayed"] += 1
            else:
                stats["on_time"] += 1
        if delays:
            stats["delay_minutes_max"] = max(delays)
            stats["delay_minutes_avg"] = sum(delays) / len(delays)
        result[str(cp)] = stats
    return result


def audit_database(db_path, now=None):
    """Open SQLite in immutable read-only mode; never create files or enable WAL."""
    path = Path(db_path).resolve(strict=True)
    uri = path.as_uri() + "?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        candidates = conn.execute(
            "SELECT id, opened_at FROM entry_discovery_candidates"
        ).fetchall()
        outcomes = conn.execute(
            "SELECT candidate_id, checkpoint_minutes, measured_at "
            "FROM entry_discovery_outcomes"
        ).fetchall()
    return audit_checkpoint_rows(candidates, outcomes, now=now)


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Read-only PAPER checkpoint timing audit")
    parser.add_argument("database", help="Path to a stopped/consistent SQLite snapshot")
    args = parser.parse_args()
    print(json.dumps(audit_database(args.database), indent=2))
