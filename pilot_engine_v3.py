"""Pilot Engine v3 — future-only PAPER challenger.

Frozen challenger rule derived from the completed OOS 1–45 diagnostic:
Price 20–39c × Early 65–69 × YES × Acceleration 0.95–1.07.

Pilot v2 remains untouched and acts as the control. No real orders are placed.
"""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone, timedelta
import math
from statistics import median
import config
from database import get_connection

VERSION = "v3-pilot-challenger"
PRICE_MIN = 0.20
PRICE_MAX = 0.399999
EARLY_MIN = 65.0
EARLY_MAX = 69.0
ACCEL_MIN = 0.95
ACCEL_MAX = 1.07
HORIZONS = (180, 360, 720, 1440)
OPEN_HORIZON_MINUTES = 1440

def _now(): return datetime.now(timezone.utc)
def _iso(dt): return dt.isoformat()
def _cfg(name, default): return getattr(config, name, default)

def ensure_schema():
    with closing(get_connection()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS pilot_engine_v3_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pilot_engine_v3_decisions(
          candidate_id INTEGER PRIMARY KEY, decided_at TEXT NOT NULL, action TEXT NOT NULL,
          reason TEXT NOT NULL, stake REAL NOT NULL, entry_yes REAL NOT NULL,
          early_score REAL NOT NULL, acceleration REAL NOT NULL, version TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_pilot_v3_decided ON pilot_engine_v3_decisions(decided_at,action);
        """)
        c.commit()

def ensure_launch():
    ensure_schema()
    with closing(get_connection()) as c:
        row=c.execute("SELECT value FROM pilot_engine_v3_meta WHERE key='launch_at'").fetchone()
        if row: return str(row[0])
        ts=_iso(_now()); c.execute("INSERT INTO pilot_engine_v3_meta(key,value) VALUES('launch_at',?)",(ts,)); c.commit(); return ts

def _realized_rows(c):
    return c.execute("""SELECT d.decided_at,d.stake,o.roi FROM pilot_engine_v3_decisions d
      JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
      WHERE d.version=? AND d.action='TRADE' AND o.checkpoint_minutes=1440
      ORDER BY d.decided_at,d.candidate_id""",(VERSION,)).fetchall()

def _realized_drawdown(c):
    equity=peak=max_dd=0.0
    for _,stake,roi in _realized_rows(c):
        equity += float(stake)*float(roi)/100.0; peak=max(peak,equity); max_dd=max(max_dd,peak-equity)
    return max_dd

def _risk_reason(c, ts):
    if _realized_drawdown(c) >= float(_cfg('PILOT_V3_MAX_DRAWDOWN_USD',100.0)): return 'KILL_SWITCH_DRAWDOWN'
    day_start=ts.replace(hour=0,minute=0,second=0,microsecond=0).isoformat()
    daily=int(c.execute("SELECT COUNT(*) FROM pilot_engine_v3_decisions WHERE version=? AND action='TRADE' AND decided_at>=? AND decided_at<=?",(VERSION,day_start,ts.isoformat())).fetchone()[0] or 0)
    if daily >= int(_cfg('PILOT_V3_MAX_NEW_TRADES_PER_DAY',8)): return 'DAILY_LIMIT'
    cutoff=(ts-timedelta(minutes=OPEN_HORIZON_MINUTES)).isoformat()
    open_n=int(c.execute("""SELECT COUNT(*) FROM pilot_engine_v3_decisions d LEFT JOIN entry_discovery_outcomes o
      ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=1440
      WHERE d.version=? AND d.action='TRADE' AND d.decided_at>=? AND d.decided_at<=? AND o.candidate_id IS NULL""",(VERSION,cutoff,ts.isoformat())).fetchone()[0] or 0)
    if open_n >= int(_cfg('PILOT_V3_MAX_OPEN_POSITIONS',5)): return 'MAX_OPEN_POSITIONS'
    return None

def process_pilot_v3_candidates(candidate_ids=None):
    launch=ensure_launch(); added=0
    with closing(get_connection()) as c:
        sql="""SELECT z.candidate_id,z.frozen_at,z.entry_yes,z.early_score,z.acceleration
          FROM stable_zone_challenger_v2_frozen z LEFT JOIN pilot_engine_v3_decisions p ON p.candidate_id=z.candidate_id
          WHERE z.version='v2-stable-zone-challenger' AND z.frozen_at>=? AND p.candidate_id IS NULL
          AND z.entry_yes>=? AND z.entry_yes<=?
          AND z.early_score>=? AND z.early_score<=?
          AND z.acceleration>=? AND z.acceleration<=?"""
        params=[launch,PRICE_MIN,PRICE_MAX,EARLY_MIN,EARLY_MAX,ACCEL_MIN,ACCEL_MAX]
        if candidate_ids:
            ph=','.join('?' for _ in candidate_ids); sql+=f" AND z.candidate_id IN ({ph})"; params.extend(int(x) for x in candidate_ids)
        sql+=' ORDER BY z.frozen_at,z.candidate_id'
        for cid,frozen_at,price,early,accel in c.execute(sql,tuple(params)).fetchall():
            try:
                ts=datetime.fromisoformat(str(frozen_at)); ts=ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
            except Exception: ts=_now()
            reason=_risk_reason(c,ts); action='SKIP' if reason else 'TRADE'; reason=reason or 'ELIGIBLE'
            stake=float(_cfg('PILOT_V3_STAKE_USD',20.0)) if action=='TRADE' else 0.0
            c.execute("""INSERT OR IGNORE INTO pilot_engine_v3_decisions
              (candidate_id,decided_at,action,reason,stake,entry_yes,early_score,acceleration,version)
              VALUES(?,?,?,?,?,?,?,?,?)""",(int(cid),str(frozen_at),action,reason,stake,float(price),float(early),float(accel),VERSION)); added+=1
        c.commit()
    return added

def _stats(rows):
    vals=[(float(s),float(r)) for s,r in rows]
    if not vals:return {'n':0,'roi':None,'pf':None,'win':None,'median':None,'pnl':0.0}
    rois=[r for _,r in vals]; pnls=[s*r/100 for s,r in vals]; gp=sum(max(x,0) for x in pnls); gl=-sum(min(x,0) for x in pnls)
    return {'n':len(vals),'roi':sum(rois)/len(rois),'pf':gp/gl if gl else (float('inf') if gp else None),'win':100*sum(r>0 for r in rois)/len(rois),'median':median(rois),'pnl':sum(pnls)}

def get_pilot_engine_v3_report():
    launch=ensure_launch(); process_pilot_v3_candidates()
    with closing(get_connection()) as c:
        counts=dict(c.execute("SELECT action,COUNT(*) FROM pilot_engine_v3_decisions WHERE version=? GROUP BY action",(VERSION,)).fetchall())
        reasons=c.execute("SELECT reason,COUNT(*) FROM pilot_engine_v3_decisions WHERE version=? AND action='SKIP' GROUP BY reason ORDER BY COUNT(*) DESC",(VERSION,)).fetchall()
        horizons={}
        for h in HORIZONS:
            rows=c.execute("""SELECT d.stake,o.roi FROM pilot_engine_v3_decisions d JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id
              WHERE d.version=? AND d.action='TRADE' AND o.checkpoint_minutes=? ORDER BY d.decided_at,d.candidate_id""",(VERSION,h)).fetchall()
            horizons[h]=_stats(rows)
        cutoff=_iso(_now()-timedelta(minutes=1440)); now=_iso(_now())
        open_n=int(c.execute("""SELECT COUNT(*) FROM pilot_engine_v3_decisions d LEFT JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=1440
          WHERE d.version=? AND d.action='TRADE' AND d.decided_at>=? AND d.decided_at<=? AND o.candidate_id IS NULL""",(VERSION,cutoff,now)).fetchone()[0] or 0)
        recent=c.execute("""SELECT e.title,d.action,d.reason,d.entry_yes,d.acceleration,d.stake FROM pilot_engine_v3_decisions d
          JOIN entry_discovery_candidates e ON e.id=d.candidate_id WHERE d.version=? ORDER BY d.candidate_id DESC LIMIT 8""",(VERSION,)).fetchall()
    return {'launch':launch,'version':VERSION,'rule':'Price 20–39¢ × Early 65–69 × YES × Acceleration 0.95–1.07','counts':counts,'reasons':reasons,'horizons':horizons,'open':open_n,'recent':recent}
