"""Entry Discovery Engine v1 — early-move Shadow/Paper candidates.

Finds modest, confirmed moves before the legacy 30%/50% momentum thresholds.
It never changes Telegram routing or Trade v2/v3. Membership is frozen at entry
and evaluated side-aware at 1/3/6/12/24h with the same 1% cost as Paper.
"""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone, timedelta
from typing import Any
import json
import math

import config
from database import get_connection

VERSION = "v1-early-entry"
CHECKPOINTS = (60, 180, 360, 720, 1440)
COOLDOWN_HOURS = 6
MAX_PER_SCAN = 12


def _now(): return datetime.now(timezone.utc)
def _f(v, default=0.0):
    try: return float(v) if v is not None else default
    except (TypeError, ValueError): return default

def ensure_schema():
    with closing(get_connection()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS entry_discovery_candidates (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          market_id TEXT NOT NULL, title TEXT NOT NULL, category TEXT,
          side TEXT NOT NULL, entry_yes REAL NOT NULL, opened_at TEXT NOT NULL,
          early_score REAL NOT NULL, reasons_json TEXT NOT NULL,
          change_5m REAL, change_15m REAL, change_1h REAL, change_24h REAL,
          volume_change REAL, liquidity_change REAL, version TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entry_discovery_market_time
          ON entry_discovery_candidates(market_id, opened_at);
        CREATE TABLE IF NOT EXISTS entry_discovery_outcomes (
          candidate_id INTEGER NOT NULL, checkpoint_minutes INTEGER NOT NULL,
          measured_at TEXT NOT NULL, exit_yes REAL NOT NULL,
          net_pnl REAL NOT NULL, roi REAL NOT NULL,
          PRIMARY KEY(candidate_id, checkpoint_minutes)
        );
        """)
        c.commit()


def _signal(m: dict[str, Any]) -> dict[str, Any] | None:
    price=_f(m.get("price")); liq=_f(m.get("liquidity"));
    if not (0.03 <= price <= 0.85) or liq < 100: return None
    c5=m.get("change_5m"); c15=m.get("change_15m"); c1=m.get("change_1h"); c24=m.get("change_24h")
    vals=[_f(x) for x in (c5,c15,c1) if x is not None]
    if not vals: return None
    # Do not chase a move that has already reached the old scanner's main thresholds.
    if c1 is not None and abs(_f(c1)) >= 25: return None
    if c24 is not None and abs(_f(c24)) >= 35: return None
    # Determine direction from the shortest useful horizon.
    lead = _f(c5) if c5 is not None and abs(_f(c5)) >= 1.5 else (_f(c15) if c15 is not None else _f(c1))
    if abs(lead) < 1.5 or abs(lead) > 15: return None
    sign=1 if lead>0 else -1
    aligned=sum(1 for x in vals if abs(x)>=1.0 and (1 if x>0 else -1)==sign)
    if aligned < 2: return None
    vol=max([_f(m.get(k), -999) for k in ("volume_change_5m","volume_change_15m","volume_change_1h")])
    lch=max([_f(m.get(k), -999) for k in ("liquidity_change_5m","liquidity_change_15m","liquidity_change_1h")])
    confirmations=0; reasons=[]; score=35.0
    if aligned>=3: score+=15; reasons.append("PRICE_ALIGNMENT")
    else: reasons.append("PRICE_ACCELERATION")
    if vol>=15: score+=20; confirmations+=1; reasons.append("VOLUME_BUILD")
    if lch>=10: score+=15; confirmations+=1; reasons.append("LIQUIDITY_BUILD")
    # moderate acceleration is preferred to an already stretched move
    strongest=max(abs(x) for x in vals)
    if 2 <= strongest <= 10: score+=15; reasons.append("PRE_BREAKOUT_MOVE")
    elif strongest<=15: score+=7
    if confirmations==0 and aligned<3: return None
    if _f(m.get("liquidity"))>=10_000: score+=5
    score=min(100.0,score)
    if score < 55: return None
    return {"side":"YES" if sign>0 else "NO","score":score,"reasons":reasons,
            "volume_change":None if vol==-999 else vol,"liquidity_change":None if lch==-999 else lch}


def process_entry_discovery(markets: list[dict[str, Any]]) -> dict[str,int]:
    """Update matured outcomes, then freeze new early candidates."""
    ensure_schema(); now=_now(); by_id={str(m.get("id")):m for m in markets if m.get("id")}
    added=0; matured=0
    cost=float(getattr(config,"PAPER_TRADING_COST_PERCENT",1.0)); stake=100.0
    with closing(get_connection()) as c:
        rows=c.execute("SELECT id,market_id,side,entry_yes,opened_at FROM entry_discovery_candidates WHERE version=?",(VERSION,)).fetchall()
        for cid,mid,side,entry,opened in rows:
            m=by_id.get(str(mid));
            if not m: continue
            try: age=(_now()-datetime.fromisoformat(str(opened))).total_seconds()/60
            except Exception: continue
            exitp=_f(m.get("price"));
            if not (0<exitp<1): continue
            for cp in CHECKPOINTS:
                if age < cp: continue
                exists=c.execute("SELECT 1 FROM entry_discovery_outcomes WHERE candidate_id=? AND checkpoint_minutes=?",(cid,cp)).fetchone()
                if exists: continue
                es=entry if side=="YES" else 1-entry; xs=exitp if side=="YES" else 1-exitp
                if es<=0: continue
                gross=(stake/es)*(xs-es); net=gross-stake*cost/100; roi=net/stake*100
                c.execute("INSERT OR IGNORE INTO entry_discovery_outcomes VALUES (?,?,?,?,?,?)",(cid,cp,now.isoformat(),exitp,net,roi)); matured+=1
        candidates=[]
        cutoff=(now-timedelta(hours=COOLDOWN_HOURS)).isoformat()
        for m in markets:
            sig=_signal(m)
            if not sig: continue
            mid=str(m.get("id") or "");
            if not mid: continue
            if c.execute("SELECT 1 FROM entry_discovery_candidates WHERE market_id=? AND opened_at>=? LIMIT 1",(mid,cutoff)).fetchone(): continue
            candidates.append((sig["score"],m,sig))
        candidates.sort(key=lambda x:x[0],reverse=True)
        for _,m,sig in candidates[:MAX_PER_SCAN]:
            c.execute("""INSERT INTO entry_discovery_candidates
              (market_id,title,category,side,entry_yes,opened_at,early_score,reasons_json,change_5m,change_15m,change_1h,change_24h,volume_change,liquidity_change,version)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (str(m.get("id")),str(m.get("title") or ""),str(m.get("category") or "OTHER"),sig["side"],_f(m.get("price")),now.isoformat(),sig["score"],json.dumps(sig["reasons"],ensure_ascii=False),m.get("change_5m"),m.get("change_15m"),m.get("change_1h"),m.get("change_24h"),sig["volume_change"],sig["liquidity_change"],VERSION)); added+=1
        c.commit()
    return {"added":added,"matured":matured}


def _stats(rows):
    vals=[float(r[0]) for r in rows]; n=len(vals); pnl=sum(float(r[1]) for r in rows)
    gp=sum(max(v,0) for v in vals); gl=-sum(min(v,0) for v in vals)
    return {"n":n,"roi":sum(vals)/n if n else None,"pnl":pnl,"win":sum(v>0 for v in vals)/n*100 if n else None,
            "pf":gp/gl if gl>0 else (float("inf") if gp>0 else None)}


def get_entry_discovery_report() -> dict[str,Any]:
    ensure_schema()
    with closing(get_connection()) as c:
        total=int(c.execute("SELECT COUNT(*) FROM entry_discovery_candidates WHERE version=?",(VERSION,)).fetchone()[0] or 0)
        sides=dict(c.execute("SELECT side,COUNT(*) FROM entry_discovery_candidates WHERE version=? GROUP BY side",(VERSION,)).fetchall())
        early={}
        for cp in CHECKPOINTS:
            early[cp]=_stats(c.execute("SELECT o.roi,o.net_pnl FROM entry_discovery_outcomes o JOIN entry_discovery_candidates e ON e.id=o.candidate_id WHERE e.version=? AND o.checkpoint_minutes=?",(VERSION,cp)).fetchall())
        # Existing delivered Paper trades are the live scanner baseline.
        old={}
        for cp in CHECKPOINTS:
            rows=c.execute("""SELECT p.stake,p.entry_price,p.trade_side,p.alert_type,o.price
              FROM paper_trades p JOIN signal_outcomes o ON o.signal_id=p.signal_id
              WHERE o.checkpoint_minutes=? AND o.status IS NOT NULL""",(cp,)).fetchall()
            vals=[]
            for stake,entry,side,atype,exitp in rows:
                st=float(stake or 100); sd=str(side or ("NO" if "DIP" in str(atype).upper() else "YES")); es=float(entry) if sd=="YES" else 1-float(entry); xs=float(exitp) if sd=="YES" else 1-float(exitp)
                if es<=0: continue
                net=(st/es)*(xs-es)-st*float(getattr(config,"PAPER_TRADING_COST_PERCENT",1.0))/100
                vals.append((net/st*100,net))
            old[cp]=_stats(vals)
        recent=c.execute("SELECT title,side,entry_yes,early_score,reasons_json FROM entry_discovery_candidates WHERE version=? ORDER BY id DESC LIMIT 5",(VERSION,)).fetchall()
    return {"version":VERSION,"total":total,"sides":sides,"early":early,"old":old,"recent":recent}


def _pct(v): return "—" if v is None else f"{v:+.1f}%"
def _pf(v):
    if v is None:return "—"
    if math.isinf(v):return "∞"
    return f"{v:.2f}"

def format_entry_discovery_report(r:dict[str,Any])->str:
    lines=["🌱 Entry Discovery v1 · SHADOW/PAPER",f"Ранних кандидатов: {r['total']} · YES {r['sides'].get('YES',0)} · NO {r['sides'].get('NO',0)}","","⚔️ A/B · EARLY ENTRY vs OLD delivered Paper"]
    labels={60:"1ч",180:"3ч",360:"6ч",720:"12ч",1440:"24ч"}
    for cp in CHECKPOINTS:
        e=r['early'][cp]; o=r['old'][cp]
        lines.append(f"• {labels[cp]} EARLY: n={e['n']} ROI {_pct(e['roi'])} PF {_pf(e['pf'])} · OLD: n={o['n']} ROI {_pct(o['roi'])} PF {_pf(o['pf'])}")
    if r['recent']:
        lines += ["","Последние EARLY:"]
        for title,side,price,score,reasons in r['recent']:
            try: rr=", ".join(json.loads(reasons or '[]')[:3])
            except Exception: rr=""
            t=str(title); t=t if len(t)<=58 else t[:57]+"…"
            lines.append(f"• {side} · {float(price)*100:.1f}¢ · Early {float(score):.0f} · {t}\n  {rr}")
    lines += ["","ℹ️ EARLY ищет умеренное согласованное движение до старых 30/50% thresholds. $100 side-aware, cost 1%. Ничего не отправляет и не меняет Trade v2/v3."]
    return "\n".join(lines)
