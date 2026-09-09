"""Rolling Edge Monitor + Strategy Regime Detector.
Shadow analytics only. Uses completed 24h AI Memory outcomes and Category v2.
Never changes live routing, Trade v2/v3, scores, or positions.
"""
from __future__ import annotations
from collections import defaultdict
from outcome_recalibration_v2 import _rows, _dimensions, _stat

WINDOWS = (100, 250, 500)
MIN_WINDOW_N = 15
TOP_ZONES = 10
REGIME_BREAK_PP = 5.0


def _zone_key(a, va, b, vb):
    return f"{a}={va} × {b}={vb}"


def _matches(row, dims, z):
    return dims[z['a']](row) == z['va'] and dims[z['b']](row) == z['vb']


def _discover_zones(rows, dims, min_n=35):
    """Discover candidates on all data only to define what to monitor.
    State itself is calculated exclusively from recent rolling windows.
    """
    names = list(dims)
    zones = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            groups = defaultdict(list)
            for r in rows:
                groups[(dims[a](r), dims[b](r))].append(r)
            for (va, vb), items in groups.items():
                if len(items) < min_n:
                    continue
                st = _stat(items)
                # Monitor historically relevant contexts, not only positive ones.
                if st['strong'] >= 15 or st['adj'] > 0:
                    zones.append({'a': a, 'va': va, 'b': b, 'vb': vb,
                                  'all_n': st['n'], 'all_adj': st['adj']})
    zones.sort(key=lambda z: (z['all_adj'], z['all_n']), reverse=True)
    return zones[:40]


def _window_stat(rows, dims, zone, size, previous=False):
    # Windows are based on market-wide completed signals, then zone membership is measured inside them.
    if previous:
        chunk = rows[-2 * size:-size] if len(rows) > size else []
    else:
        chunk = rows[-size:]
    items = [r for r in chunk if _matches(r, dims, zone)]
    return _stat(items) if items else {'n': 0, 'avg': 0.0, 'adj': None, 'strong': 0.0}


def _state(cur, prev):
    cn, pn = cur['n'], prev['n']
    ca, pa = cur.get('adj'), prev.get('adj')
    if cn < MIN_WINDOW_N:
        return '⏳ INSUFFICIENT'
    if pn < MIN_WINDOW_N or pa is None:
        return '🟡 EMERGING' if ca is not None and ca > 0 else '🔴 DEAD'
    if ca is not None and ca > 0:
        if pa > 0:
            return '🟢 ACTIVE'
        return '🟡 EMERGING'
    if pa > 0:
        return '🟠 FADING'
    return '🔴 DEAD'


def _break(cur, prev):
    if cur['n'] < MIN_WINDOW_N or prev['n'] < MIN_WINDOW_N:
        return False, None
    ca, pa = cur.get('adj'), prev.get('adj')
    if ca is None or pa is None:
        return False, None
    delta = ca - pa
    return (pa > 0 >= ca and delta <= -REGIME_BREAK_PP), delta


def get_rolling_edge_report(minutes=1440, limit=10000):
    rows = _rows(minutes, limit)
    rows.sort(key=lambda r: r.get('created_at', ''))
    dims = _dimensions()
    zones = _discover_zones(rows, dims)
    out = []
    for z in zones:
        stats = {}
        for w in WINDOWS:
            cur = _window_stat(rows, dims, z, w, False)
            prev = _window_stat(rows, dims, z, w, True)
            brk, delta = _break(cur, prev)
            stats[w] = {'current': cur, 'previous': prev, 'state': _state(cur, prev),
                        'regime_break': brk, 'delta': delta}
        # Primary state uses 500 when enough samples, then 250, then 100.
        primary = None
        for w in (500, 250, 100):
            if stats[w]['current']['n'] >= MIN_WINDOW_N:
                primary = w
                break
        if primary is None:
            continue
        p = stats[primary]
        out.append({**z, 'stats': stats, 'primary_window': primary,
                    'state': p['state'], 'regime_break': any(x['regime_break'] for x in stats.values())})
    rank = {'🟢 ACTIVE': 0, '🟡 EMERGING': 1, '🟠 FADING': 2, '🔴 DEAD': 3, '⏳ INSUFFICIENT': 4}
    out.sort(key=lambda z: (rank.get(z['state'], 9), -(z['stats'][z['primary_window']]['current'].get('adj') or -999)))
    return {'minutes': minutes, 'n': len(rows), 'zones': out[:TOP_ZONES],
            'counts': dict(__import__('collections').Counter(z['state'] for z in out)),
            'category_mode': 'v2 title reclassification'}


def _pct(v):
    return '—' if v is None else f"{v:+.1f}%"


def format_rolling_edge_report(r):
    lines = [f"📡 Rolling Edge Monitor · {int(r['minutes']/60)}ч",
             f"База {r['n']} · окна 100/250/500 сигналов · Category v2", ""]
    c = r.get('counts', {})
    lines.append("Состояния: " + " · ".join(f"{k}×{v}" for k, v in c.items()) if c else "Состояния: —")
    lines.append("")
    if not r['zones']:
        lines.append("Пока нет зон с достаточной выборкой в rolling-окнах.")
    for z in r['zones']:
        w = z['primary_window']; s = z['stats'][w]
        cur, prev = s['current'], s['previous']
        mark = " · ⚡ REGIME BREAK" if z['regime_break'] else ""
        lines.append(f"• {_zone_key(z['a'],z['va'],z['b'],z['vb'])} · {z['state']}{mark}")
        lines.append(f"  W{w}: current n={cur['n']} adj {_pct(cur.get('adj'))} · previous n={prev['n']} adj {_pct(prev.get('adj'))}")
        if s.get('delta') is not None:
            lines.append(f"  Δ edge {_pct(s['delta'])} · all-time n={z['all_n']} {_pct(z['all_adj'])}")
    lines += ["", "ℹ️ ACTIVE/EMERGING/FADING/DEAD оцениваются только по rolling-окнам. Shadow analytics; live-логика не меняется."]
    return '\n'.join(lines)


def get_strategy_regime_report(minutes=1440, limit=10000):
    rows = _rows(minutes, limit)
    rows.sort(key=lambda r: r.get('created_at', ''))
    dims = _dimensions()
    # Broad strategy families: enough to identify market-wide breaks without overfitting a single pair.
    families = [
        ('ALL', lambda r: True),
        ('CRYPTO', lambda r: dims['Category'](r) == 'CRYPTO'),
        ('CRYPTO PUMP', lambda r: dims['Category'](r) == 'CRYPTO' and dims['Direction'](r) == 'PUMP'),
        ('PUMP', lambda r: dims['Direction'](r) == 'PUMP'),
        ('DIP', lambda r: dims['Direction'](r) == 'DIP'),
    ]
    result = []
    w = 500
    for name, fn in families:
        current_chunk = rows[-w:]
        previous_chunk = rows[-2*w:-w]
        cur_items = [r for r in current_chunk if fn(r)]
        prev_items = [r for r in previous_chunk if fn(r)]
        cur = _stat(cur_items) if cur_items else {'n':0,'adj':None,'strong':0}
        prev = _stat(prev_items) if prev_items else {'n':0,'adj':None,'strong':0}
        brk, delta = _break(cur, prev)
        state = _state(cur, prev)
        result.append({'name': name, 'current': cur, 'previous': prev, 'state': state,
                       'regime_break': brk, 'delta': delta})
    return {'minutes': minutes, 'n': len(rows), 'window': w, 'families': result}


def format_strategy_regime_report(r):
    lines = ["🌐 Strategy Regime Detector", f"24ч outcomes · current/previous W{r['window']} market signals", ""]
    for x in r['families']:
        c, p = x['current'], x['previous']
        mark = " ⚡ REGIME BREAK" if x['regime_break'] else ""
        lines.append(f"• {x['name']} · {x['state']}{mark}")
        lines.append(f"  current n={c['n']} adj {_pct(c.get('adj'))} · previous n={p['n']} adj {_pct(p.get('adj'))} · Δ {_pct(x.get('delta'))}")
    lines += ["", "ℹ️ Detector ищет смену эффективности стратегии во времени. Ничего не блокирует и не меняет Trade v2/v3."]
    return '\n'.join(lines)
