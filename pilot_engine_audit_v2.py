"""Read-only audit for Pilot v2 MAX_OPEN skips.

Compares actual Pilot v2 TRADE decisions with otherwise eligible candidates that
were skipped only because MAX_OPEN_POSITIONS was reached. No decisions/orders
are created or modified here.
"""
from __future__ import annotations
from contextlib import closing
import math
from statistics import median
import config
from database import get_connection
from pilot_engine_v2 import VERSION, HORIZONS


def _cfg(name, default):
    return getattr(config, name, default)


def _stats(rows):
    stake = float(_cfg('PILOT_V2_STAKE_USD', 20.0))
    rois = [float(r[0]) for r in rows if r[0] is not None]
    if not rois:
        return {'n': 0, 'roi': None, 'pf': None, 'win': None, 'median': None, 'pnl': 0.0}
    pnls = [stake * r / 100.0 for r in rois]
    gp = sum(max(x, 0.0) for x in pnls)
    gl = -sum(min(x, 0.0) for x in pnls)
    return {
        'n': len(rois), 'roi': sum(rois) / len(rois),
        'pf': gp / gl if gl else (float('inf') if gp else None),
        'win': 100.0 * sum(r > 0 for r in rois) / len(rois),
        'median': median(rois), 'pnl': sum(pnls),
    }


def get_pilot_engine_audit_v2_report():
    with closing(get_connection()) as c:
        counts = dict(c.execute(
            "SELECT action,COUNT(*) FROM pilot_engine_v2_decisions WHERE version=? GROUP BY action",
            (VERSION,),
        ).fetchall())
        max_open_total = int(c.execute(
            "SELECT COUNT(*) FROM pilot_engine_v2_decisions WHERE version=? AND action='SKIP' AND reason='MAX_OPEN_POSITIONS'",
            (VERSION,),
        ).fetchone()[0] or 0)
        horizons = {}
        for h in HORIZONS:
            trade_rows = c.execute("""SELECT o.roi FROM pilot_engine_v2_decisions d
                JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
                WHERE d.version=? AND d.action='TRADE' AND o.checkpoint_minutes=?
                ORDER BY d.decided_at,d.candidate_id""", (VERSION, h)).fetchall()
            skip_rows = c.execute("""SELECT o.roi FROM pilot_engine_v2_decisions d
                JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
                WHERE d.version=? AND d.action='SKIP' AND d.reason='MAX_OPEN_POSITIONS'
                AND o.checkpoint_minutes=? ORDER BY d.decided_at,d.candidate_id""", (VERSION, h)).fetchall()
            t, s = _stats(trade_rows), _stats(skip_rows)
            horizons[h] = {
                'trade': t, 'max_open_skip': s,
                'roi_delta_skip_minus_trade': None if t['roi'] is None or s['roi'] is None else s['roi'] - t['roi'],
            }
        recent = c.execute("""SELECT e.title,d.decided_at,d.entry_yes,d.early_score,d.acceleration
            FROM pilot_engine_v2_decisions d JOIN entry_discovery_candidates e ON e.id=d.candidate_id
            WHERE d.version=? AND d.action='SKIP' AND d.reason='MAX_OPEN_POSITIONS'
            ORDER BY d.decided_at DESC,d.candidate_id DESC LIMIT 10""", (VERSION,)).fetchall()
    return {
        'mode': 'READ_ONLY', 'experiment': 'pilot_v2_max_open_audit',
        'trade_total': int(counts.get('TRADE', 0)), 'max_open_skip_total': max_open_total,
        'hypothetical_stake': float(_cfg('PILOT_V2_STAKE_USD', 20.0)),
        'horizons': horizons, 'recent_max_open_skips': recent,
        'note': 'MAX_OPEN skips are hypothetical only; Pilot v2 decisions are not changed.',
    }
