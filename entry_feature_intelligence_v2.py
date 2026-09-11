"""Entry Feature Intelligence v2 — walk-forward validation + future-only challenger.

Shadow/Paper only. Uses features frozen by Entry Feature Recorder v1.
No live routing/trading decisions are changed.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import math

from database import get_connection

VERSION = "v2-entry-feature-intelligence"
CHECKPOINTS = (180, 360, 1440)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v, default=None):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def ensure_schema() -> None:
    with closing(get_connection()) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS entry_feature_intel_meta(
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entry_feature_v2_frozen(
              candidate_id INTEGER PRIMARY KEY,
              frozen_at TEXT NOT NULL,
              feature_score REAL NOT NULL,
              tier TEXT NOT NULL,
              eligible INTEGER NOT NULL,
              reasons TEXT,
              version TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_entry_feature_v2_tier
              ON entry_feature_v2_frozen(tier, eligible);
            """
        )
        c.commit()


def ensure_launch() -> str:
    ensure_schema()
    with closing(get_connection()) as c:
        row = c.execute(
            "SELECT value FROM entry_feature_intel_meta WHERE key='launch_at'"
        ).fetchone()
        if row:
            return str(row[0])
        now = _now()
        c.execute(
            "INSERT INTO entry_feature_intel_meta(key,value) VALUES('launch_at',?)",
            (now,),
        )
        c.commit()
        return now


def _bucket_price(p: float) -> str:
    if p < 0.05:
        return "<5¢"
    if p < 0.20:
        return "5–20¢"
    if p < 0.50:
        return "20–50¢"
    return "≥50¢"


def _feature_keys(row) -> list[str]:
    (
        _cid, _ts, side, price, early, acc, vol, liq, spread, bal,
        imbalance, same, age, dlow, dhigh,
    ) = row
    p = _f(price, 0.0)
    es = _f(early, 0.0)
    keys = [
        f"Side={side}",
        f"Price={_bucket_price(p)}",
        f"Early={'80+' if es >= 80 else '70–79' if es >= 70 else '60–69' if es >= 60 else '<60'}",
    ]
    if acc is not None:
        keys.append("Acceleration=UP" if float(acc) > 0.05 else "Acceleration=FLAT/DOWN")
    if vol is not None:
        v = float(vol)
        keys.append("VolumeΔ=300+" if v >= 300 else "VolumeΔ=80–299" if v >= 80 else "VolumeΔ=<80")
    if liq is not None:
        l = float(liq)
        keys.append("LiquidityΔ=30+" if l >= 30 else "LiquidityΔ=<30")
    if spread is not None:
        s = float(spread)
        keys.append("Spread=<2¢" if s < 0.02 else "Spread=2–5¢" if s < 0.05 else "Spread=5¢+")
    if bal is not None:
        keys.append("BidBalance=40%+" if float(bal) >= 40 else "BidBalance=<40%")
    if imbalance is not None:
        im = float(imbalance)
        keys.append("DepthImbalance=BID" if im >= 20 else "DepthImbalance=ASK" if im <= -20 else "DepthImbalance=BALANCED")
    if same is not None:
        keys.append(f"Alignment={'3' if int(same) >= 3 else '2' if int(same) == 2 else '<2'}")
    if age is not None:
        a = float(age)
        keys.append("MoveAge=≤15m" if a <= 15 else "MoveAge=16–60m" if a <= 60 else "MoveAge=>60m")
    if dlow is not None:
        dl = float(dlow)
        keys.append("From1hLow=<5%" if dl < 5 else "From1hLow=5–15%" if dl < 15 else "From1hLow=15%+")
    if dhigh is not None:
        dh = float(dhigh)
        keys.append("To1hHigh=>-5%" if dh > -5 else "To1hHigh=-15…-5%" if dh > -15 else "To1hHigh=<-15%")
    return keys


def _score_snapshot(row) -> tuple[float, list[str]]:
    """Pre-registered score based only on features available at entry.

    We intentionally favor the future-only patterns that motivated this module,
    but keep the model fixed after launch so outcomes cannot alter past scores.
    """
    (
        _cid, _ts, side, price, early, acc, vol, liq, spread, bal,
        imbalance, same, age, dlow, dhigh,
    ) = row
    p = _f(price, 0.0)
    es = _f(early, 0.0)
    s = 42.0
    why: list[str] = []

    if acc is not None and float(acc) > 0.05:
        s += 18; why.append("ACCEL_UP")
    elif acc is not None:
        s -= 4; why.append("ACCEL_FLAT")

    if p < 0.05:
        s += 14; why.append("PRICE_LT5")
    elif 0.20 <= p < 0.50:
        s += 5; why.append("PRICE_20_50")
    elif p >= 0.50:
        s -= 7; why.append("PRICE_HIGH")

    if spread is not None:
        sp = float(spread)
        if sp < 0.02:
            s += 9; why.append("TIGHT_SPREAD")
        elif sp >= 0.05:
            s -= 2; why.append("WIDE_SPREAD")

    if bal is not None:
        bb = float(bal)
        if bb < 40:
            s += 8; why.append("LOW_BID_BAL")
        else:
            s -= 10; why.append("HIGH_BID_BAL")
            if p >= 0.50:
                s -= 8; why.append("HIGH_PRICE_X_BID")

    if vol is not None and float(vol) >= 80:
        s += 4; why.append("VOL_BUILD")
    if liq is not None and float(liq) >= 30:
        s += 2; why.append("LIQ_BUILD")
    if same is not None and int(same) >= 3:
        s += 4; why.append("ALIGN_3")

    # Existing Early score is only a weak context input; it was not monotonic.
    if 60 <= es < 70:
        s += 4; why.append("EARLY_60_69")
    elif es >= 80:
        s -= 2; why.append("EARLY_80_PLUS")

    return max(0.0, min(100.0, s)), why


def freeze_feature_candidates(candidate_ids: list[int] | None = None) -> int:
    launch = ensure_launch()
    now = _now()
    added = 0
    with closing(get_connection()) as c:
        sql = """SELECT f.candidate_id,f.captured_at,f.side,f.entry_yes,f.early_score,
          f.acceleration,f.volume_change,f.liquidity_change,f.spread,f.bid_balance,
          f.depth_imbalance,f.same_direction_count,f.move_age_minutes,
          f.distance_1h_low_pct,f.distance_1h_high_pct
          FROM entry_feature_snapshots f
          LEFT JOIN entry_feature_v2_frozen v ON v.candidate_id=f.candidate_id
          WHERE v.candidate_id IS NULL AND f.captured_at>=?"""
        params: list[object] = [launch]
        if candidate_ids:
            ph = ",".join("?" for _ in candidate_ids)
            sql += f" AND f.candidate_id IN ({ph})"
            params.extend(int(x) for x in candidate_ids)
        sql += " ORDER BY f.candidate_id"
        rows = c.execute(sql, tuple(params)).fetchall()
        for row in rows:
            sc, why = _score_snapshot(row)
            tier = "80+" if sc >= 80 else "70–79" if sc >= 70 else "60–69" if sc >= 60 else "<60"
            eligible = 1 if sc >= 70 else 0
            c.execute(
                """INSERT OR IGNORE INTO entry_feature_v2_frozen
                (candidate_id,frozen_at,feature_score,tier,eligible,reasons,version)
                VALUES(?,?,?,?,?,?,?)""",
                (int(row[0]), now, sc, tier, eligible, ",".join(why[:8]), VERSION),
            )
            added += 1
        c.commit()
    return added


def _stats(vals: list[float]) -> dict:
    vals = [float(v) for v in vals]
    n = len(vals)
    if not n:
        return {"n": 0, "roi": None, "pf": None, "win": None}
    gp = sum(max(v, 0.0) for v in vals)
    gl = -sum(min(v, 0.0) for v in vals)
    return {
        "n": n,
        "roi": sum(vals) / n,
        "pf": gp / gl if gl else (float("inf") if gp else None),
        "win": sum(v > 0 for v in vals) / n * 100.0,
    }


def _fmt(s: dict) -> str:
    roi = "—" if s["roi"] is None else f"{s['roi']:+.1f}%"
    pf = "—" if s["pf"] is None else ("∞" if math.isinf(s["pf"]) else f"{s['pf']:.2f}")
    return f"n={s['n']} ROI {roi} PF {pf}"


def _load_rows(cp: int):
    with closing(get_connection()) as c:
        return c.execute(
            """SELECT f.candidate_id,f.captured_at,f.side,f.entry_yes,f.early_score,
              f.acceleration,f.volume_change,f.liquidity_change,f.spread,f.bid_balance,
              f.depth_imbalance,f.same_direction_count,f.move_age_minutes,
              f.distance_1h_low_pct,f.distance_1h_high_pct,o.roi
              FROM entry_feature_snapshots f
              JOIN entry_discovery_outcomes o ON o.candidate_id=f.candidate_id
              WHERE o.checkpoint_minutes=?
              ORDER BY f.captured_at,f.candidate_id""",
            (cp,),
        ).fetchall()


def _walk_forward(cp: int) -> dict:
    rows = _load_rows(cp)
    enriched = []
    for r in rows:
        base = tuple(r[:-1])
        y = float(r[-1])
        keys = _feature_keys(base)
        enriched.append((keys, y))
    n = len(enriched)
    a = int(n * 0.60)
    b = int(n * 0.80)

    # Pre-registered single/two/three-feature interactions focused on raw entry state.
    segments: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for i, (keys, y) in enumerate(enriched):
        for k in keys:
            segments[k].append((i, y))
        focus = [k for k in keys if k.startswith(("Acceleration=", "Price=", "Spread=", "BidBalance=", "VolumeΔ=", "LiquidityΔ=", "Early=", "Side="))]
        for x in range(len(focus)):
            for z in range(x + 1, len(focus)):
                segments[f"{focus[x]} × {focus[z]}"].append((i, y))
        # Only a small core of three-way combinations to avoid combinatorial fishing.
        acc = next((k for k in keys if k.startswith("Acceleration=")), None)
        price = next((k for k in keys if k.startswith("Price=")), None)
        spread = next((k for k in keys if k.startswith("Spread=")), None)
        bal = next((k for k in keys if k.startswith("BidBalance=")), None)
        for trio in ((acc, price, spread), (acc, price, bal), (price, spread, bal)):
            if all(trio):
                segments[" × ".join(trio)].append((i, y))

    zones = []
    for name, ivals in segments.items():
        if len(ivals) < 30:
            continue
        d = [y for i, y in ivals if i < a]
        v = [y for i, y in ivals if a <= i < b]
        h = [y for i, y in ivals if i >= b]
        ds, vs, hs = _stats(d), _stats(v), _stats(h)
        if ds["n"] < 15 or ds["roi"] is None or ds["roi"] <= 0:
            continue
        state = "✅ STABLE" if vs["n"] >= 10 and hs["n"] >= 10 and (vs["roi"] or -999) > 0 and (hs["roi"] or -999) > 0 else "⚠️ UNCONFIRMED"
        zones.append((state, name, ds, vs, hs))
    zones.sort(key=lambda z: (z[0] == "✅ STABLE", z[2]["roi"] or -999), reverse=True)
    return {"n": n, "zones": zones[:10]}


def get_feature_intelligence_report(cp: int = 360) -> dict:
    freeze_feature_candidates()
    wf = _walk_forward(cp)
    with closing(get_connection()) as c:
        frozen = c.execute(
            """SELECT v.tier,v.eligible,o.roi FROM entry_feature_v2_frozen v
            JOIN entry_discovery_outcomes o ON o.candidate_id=v.candidate_id
            WHERE v.version=? AND o.checkpoint_minutes=?""",
            (VERSION, cp),
        ).fetchall()
        total_frozen = c.execute(
            "SELECT COUNT(*) FROM entry_feature_v2_frozen WHERE version=?", (VERSION,)
        ).fetchone()[0]
        launch = c.execute(
            "SELECT value FROM entry_feature_intel_meta WHERE key='launch_at'"
        ).fetchone()[0]
    tiers: dict[str, list[float]] = defaultdict(list)
    eligible: list[float] = []
    for tier, el, roi in frozen:
        tiers[str(tier)].append(float(roi))
        if int(el):
            eligible.append(float(roi))
    return {
        "cp": cp,
        "n": wf["n"],
        "zones": wf["zones"],
        "frozen": int(total_frozen or 0),
        "frozen_outcomes": len(frozen),
        "eligible": _stats(eligible),
        "tiers": {k: _stats(v) for k, v in tiers.items()},
        "launch": launch,
    }


def format_feature_intelligence_report(r: dict) -> str:
    label = {180: "3ч", 360: "6ч", 1440: "24ч"}[r["cp"]]
    lines = [
        f"🧬 Entry Feature Intelligence v2 · {label}",
        f"Recorder outcomes: {r['n']} · chronological 60/20/20",
        "",
        "🔬 Feature Walk-Forward",
    ]
    if not r["zones"]:
        lines.append("• Положительных Discovery-зон с достаточной выборкой пока нет")
    else:
        for state, name, d, v, h in r["zones"]:
            lines.append(f"• {name} · {state}\n  D {_fmt(d)} · V {_fmt(v)} · H {_fmt(h)}")
    lines += [
        "",
        "🧪 Feature Challenger · FUTURE-ONLY",
        f"Frozen candidates: {r['frozen']} · matured: {r['frozen_outcomes']}",
        f"Eligible Feature Score≥70: {_fmt(r['eligible'])}",
    ]
    for tier in ("80+", "70–79", "60–69", "<60"):
        if tier in r["tiers"]:
            lines.append(f"• Score {tier}: {_fmt(r['tiers'][tier])}")
    lines += [
        "",
        "ℹ️ Feature Score фиксируется при EARLY-входе и не меняет Entry v1/v2, Trade v2/v3 или Live. Walk-Forward использует только frozen Entry Feature Recorder data.",
    ]
    return "\n".join(lines)
