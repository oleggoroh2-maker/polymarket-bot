"""Read-only, exploratory Pilot v2 category-filter counterfactual (24h outcomes)."""
import re
from collections import Counter
from contextlib import closing
from database import get_connection
from pilot_engine_v2 import VERSION
from pilot_market_regime_audit_v2 import PATTERNS, category, stats

EXCLUDED = frozenset(('CRYPTO', 'SPORTS', 'AI_TECH'))

def report():
    with closing(get_connection()) as conn:
        rows = conn.execute('''SELECT d.candidate_id,d.decided_at,d.stake,e.title,e.category,o.roi
          FROM pilot_engine_v2_decisions d JOIN entry_discovery_candidates e ON e.id=d.candidate_id
          LEFT JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=1440
          WHERE d.version=? AND d.action='TRADE' ORDER BY d.decided_at,d.candidate_id''', (VERSION,)).fetchall()
    baseline, retained, removed, flags = [], [], [], []
    for cid, dt, stake, title, raw, roi in rows:
        matches = [name for name, pattern in PATTERNS if re.search(pattern, str(title or ''), re.I)]
        assigned = category(title, raw)
        excluded = assigned in EXCLUDED
        # Conservative ambiguity: even a non-excluded assigned category may contain an excluded keyword.
        conflicting = bool(set(matches) & EXCLUDED and not excluded)
        if len(matches) > 1 or conflicting:
            flags.append(dict(candidate_id=str(cid), title=str(title or '')[:180], source_category=str(raw or ''), assigned=assigned, matches=matches, excluded_by_current_rule=excluded))
        if roi is None:
            continue
        item = (stake, roi)
        baseline.append(item)
        (removed if excluded else retained).append(item)
    def summary(items):
        s = stats(items)
        ordered = sorted(items, key=lambda x: x[1])
        s['without_best_roi'] = stats(ordered[:-1])['roi'] if len(ordered) > 1 else None
        s['without_worst_roi'] = stats(ordered[1:])['roi'] if len(ordered) > 1 else None
        return s
    return dict(mode='READ_ONLY', total_trades=len(rows), matured=len(baseline), excluded_categories=sorted(EXCLUDED),
                baseline=summary(baseline), retained=summary(retained), removed=summary(removed),
                excluded_count=len(removed), flagged=flags[:100], flagged_total=len(flags),
                note='Exploratory selection on historical Pilot v2 trades, NOT a new backtest or future OOS validation. Removing trades changes capital occupancy and subsequent MAX_OPEN selection; the displayed subset cannot simulate those replacement trades. First-match keyword classification can mislabel markets. PAPER returns exclude fees, slippage and fill risk. No decisions, database, trading rules or Telegram changed.')
