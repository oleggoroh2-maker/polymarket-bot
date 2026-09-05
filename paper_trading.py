"""Paper Trading v2 — side-aware virtual PnL + audit + market regimes.

No real orders. No effect on alert selection. PUMP is modeled as buying YES;
DIP is modeled as buying NO. This is deliberately different from AI Memory's
symmetric directional-return metric and reflects a tradable binary position.
"""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
from typing import Any
import json
import config
from database import get_connection
from market_regime_engine import classify_market_regime

CHECKPOINTS = ((60, "1ч"), (180, "3ч"), (360, "6ч"), (720, "12ч"), (1440, "24ч"))

def _is_dip(alert_type: str) -> bool:
    x = str(alert_type or "").upper()
    return any(k in x for k in ("DIP", "DROP", "BEAR"))

def _side(alert_type: str) -> str:
    return "NO" if _is_dip(alert_type) else "YES"

def _side_price(yes_price: float, side: str) -> float:
    p = min(0.999999, max(0.000001, float(yes_price)))
    return (1.0 - p) if side == "NO" else p

def _trade_math(stake: float, entry_yes: float, exit_yes: float, side: str, cost_pct: float) -> dict[str, float]:
    entry_side = _side_price(entry_yes, side)
    exit_side = _side_price(exit_yes, side)
    shares = stake / entry_side
    gross = shares * (exit_side - entry_side)
    costs = stake * cost_pct / 100.0
    net = gross - costs
    return {"entry_side": entry_side, "exit_side": exit_side, "shares": shares,
            "gross_pnl": gross, "costs": costs, "net_pnl": net,
            "roi": net / stake * 100.0 if stake else 0.0}

def ensure_paper_schema() -> None:
    with closing(get_connection()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            signal_id TEXT PRIMARY KEY, opened_at TEXT NOT NULL, stake REAL NOT NULL,
            entry_price REAL NOT NULL, category TEXT, alert_type TEXT, title TEXT,
            final_signal REAL, ev_estimate REAL, risk_score REAL
        );
        CREATE INDEX IF NOT EXISTS idx_paper_trades_opened ON paper_trades(opened_at);
        """)
        cols={r[1] for r in c.execute("PRAGMA table_info(paper_trades)").fetchall()}
        additions={"trade_side":"TEXT", "market_regime":"TEXT", "regime_confidence":"REAL", "regime_reasons":"TEXT", "risk_stake":"REAL", "entry_quality":"REAL", "chase_risk":"REAL", "trade_intelligence_version":"TEXT", "trade_v2_decision":"TEXT", "trade_v2_skip_reasons":"TEXT", "trade_v2_exit_minutes":"INTEGER", "news_status":"TEXT", "news_score":"REAL", "news_direction":"TEXT", "news_freshest_hours":"REAL", "news_source_count":"INTEGER", "social_mentions":"INTEGER"}
        for name, typ in additions.items():
            if name not in cols: c.execute(f"ALTER TABLE paper_trades ADD COLUMN {name} {typ}")
        c.commit()

def record_delivered_trade(alert: dict[str, Any]) -> bool:
    if not bool(getattr(config, "PAPER_TRADING_MODE", True)): return False
    signal_id=str(alert.get("ai_signal_id") or "").strip()
    if not signal_id: return False
    ensure_paper_schema()
    stake=float(getattr(config,"PAPER_TRADE_STAKE_USD",100.0))
    entry=float(alert.get("current_price",alert.get("price")) or 0.0)
    alert_type=str(alert.get("alert_type") or "")
    side=_side(alert_type)
    regime=classify_market_regime(alert)
    risk_raw=alert.get("suggested_stake_usd")
    risk_stake=float(stake if risk_raw is None else risk_raw)
    entry_quality=float(alert.get("entry_quality_score") or 50.0)
    chase_risk=float(alert.get("chase_risk_score") or 50.0)
    ti_version=str(alert.get("trade_intelligence_version") or "legacy")
    with closing(get_connection()) as c:
        cur=c.execute("""INSERT OR IGNORE INTO paper_trades
        (signal_id,opened_at,stake,entry_price,category,alert_type,title,final_signal,ev_estimate,risk_score,
         trade_side,market_regime,regime_confidence,regime_reasons,risk_stake,entry_quality,chase_risk,trade_intelligence_version,trade_v2_decision,trade_v2_skip_reasons,trade_v2_exit_minutes,news_status,news_score,news_direction,news_freshest_hours,news_source_count,social_mentions)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(
            signal_id,datetime.now(timezone.utc).isoformat(),stake,entry,
            str(alert.get("category") or "OTHER"),alert_type,str(alert.get("title") or ""),
            float(alert.get("final_signal_score") or 0),float(alert.get("ev_estimate_percent") or 0),
            float(alert.get("risk_score") or 0),side,regime["regime"],regime["confidence"],json.dumps(regime["reasons"],ensure_ascii=False),risk_stake,entry_quality,chase_risk,ti_version,str(alert.get("trade_v2_decision") or "LEGACY"),json.dumps(alert.get("trade_v2_skip_reasons") or [],ensure_ascii=False),int(alert.get("trade_v2_exit_minutes") or 360),str(alert.get("news_status") or "UNKNOWN"),float(alert.get("news_score") or 0),str(alert.get("news_direction") or "NEUTRAL"),alert.get("news_freshest_hours"),int(alert.get("news_source_count") or 0),int(alert.get("social_mentions") or 0)))
        c.commit(); return cur.rowcount>0

def _rows(c, minutes:int, where:str="", args:tuple=()):
    return c.execute(f"""SELECT p.signal_id,p.stake,p.entry_price,p.alert_type,p.trade_side,p.category,p.title,
        p.market_regime,o.price,o.return_percent,o.directional_return_percent,o.status,o.measured_at
        FROM paper_trades p JOIN signal_outcomes o ON o.signal_id=p.signal_id
        WHERE o.checkpoint_minutes=? AND o.status IS NOT NULL {where}""",(minutes,*args)).fetchall()

def _stats_from_rows(rows) -> dict[str,Any]:
    cost=float(getattr(config,"PAPER_TRADING_COST_PERCENT",1.0)); pnls=[]; raw=[]
    for r in rows:
        m=_trade_math(float(r[1]),float(r[2]),float(r[8]),str(r[4] or _side(r[3])),cost)
        pnls.append(m["net_pnl"]); raw.append(m["gross_pnl"])
    invested=sum(float(r[1]) for r in rows); wins=sum(x>0 for x in pnls)
    gp=sum(x for x in pnls if x>0); gl=-sum(x for x in pnls if x<0)
    return {"n":len(rows),"raw_pnl":sum(raw),"pnl":sum(pnls),"roi":sum(pnls)/invested*100 if invested else None,
            "win_rate":wins/len(rows)*100 if rows else None,"profit_factor":gp/gl if gl>0 else (float("inf") if rows and gp>0 else None)}

def _risk_stats(c, minutes:int)->dict[str,Any]:
    rows=c.execute("""SELECT p.stake,p.entry_price,p.alert_type,p.trade_side,o.price,p.risk_stake,p.trade_intelligence_version
        FROM paper_trades p JOIN signal_outcomes o ON o.signal_id=p.signal_id
        WHERE o.checkpoint_minutes=? AND o.status IS NOT NULL AND p.risk_stake IS NOT NULL AND p.trade_intelligence_version='v1'""",(minutes,)).fetchall()
    cost=float(getattr(config,"PAPER_TRADING_COST_PERCENT",1.0)); pnls=[]; invested=0.0
    for r in rows:
        stake=float(r[5]); m=_trade_math(stake,float(r[1]),float(r[4]),str(r[3] or _side(r[2])),cost)
        pnls.append(m["net_pnl"]); invested+=stake
    gp=sum(x for x in pnls if x>0); gl=-sum(x for x in pnls if x<0)
    return {"n":len(rows),"pnl":sum(pnls),"roi":sum(pnls)/invested*100 if invested else None,"win_rate":sum(x>0 for x in pnls)/len(rows)*100 if rows else None,"profit_factor":gp/gl if gl>0 else (float("inf") if rows and gp>0 else None),"invested":invested}

def _trade_v2_stats(c, minutes:int)->dict[str,Any]:
    rows=c.execute("""SELECT p.entry_price,p.alert_type,p.trade_side,o.price,p.risk_stake,p.trade_v2_decision,p.trade_intelligence_version
        FROM paper_trades p JOIN signal_outcomes o ON o.signal_id=p.signal_id
        WHERE o.checkpoint_minutes=? AND o.status IS NOT NULL AND p.trade_intelligence_version='v2' AND p.trade_v2_decision='TRADE'""",(minutes,)).fetchall()
    cost=float(getattr(config,"PAPER_TRADING_COST_PERCENT",1.0)); pnls=[]; invested=0.0
    for r in rows:
        stake=float(r[4] or 0); m=_trade_math(stake,float(r[0]),float(r[3]),str(r[2] or _side(r[1])),cost)
        pnls.append(m["net_pnl"]); invested+=stake
    gp=sum(x for x in pnls if x>0); gl=-sum(x for x in pnls if x<0)
    return {"n":len(rows),"pnl":sum(pnls),"roi":sum(pnls)/invested*100 if invested else None,"win_rate":sum(x>0 for x in pnls)/len(rows)*100 if rows else None,"profit_factor":gp/gl if gl>0 else (float("inf") if rows and gp>0 else None),"invested":invested}

def _trade_v2_exit_stats(c)->dict[str,Any]:
    rows=c.execute("""SELECT p.entry_price,p.alert_type,p.trade_side,p.risk_stake,p.trade_v2_exit_minutes,o.price
        FROM paper_trades p JOIN signal_outcomes o ON o.signal_id=p.signal_id AND o.checkpoint_minutes=p.trade_v2_exit_minutes
        WHERE o.status IS NOT NULL AND p.trade_intelligence_version='v2' AND p.trade_v2_decision='TRADE'""").fetchall()
    cost=float(getattr(config,"PAPER_TRADING_COST_PERCENT",1.0)); pnls=[]; invested=0.0
    for r in rows:
        stake=float(r[3] or 0); m=_trade_math(stake,float(r[0]),float(r[5]),str(r[2] or _side(r[1])),cost)
        pnls.append(m["net_pnl"]); invested+=stake
    gp=sum(x for x in pnls if x>0); gl=-sum(x for x in pnls if x<0)
    return {"n":len(rows),"pnl":sum(pnls),"roi":sum(pnls)/invested*100 if invested else None,"win_rate":sum(x>0 for x in pnls)/len(rows)*100 if rows else None,"profit_factor":gp/gl if gl>0 else (float("inf") if rows and gp>0 else None),"invested":invested}

def _trade_v2_diagnostics(c)->dict[str,Any]:
    base="p.trade_intelligence_version='v2'"
    total=int(c.execute(f"SELECT COUNT(*) FROM paper_trades p WHERE {base}").fetchone()[0] or 0)
    traded=int(c.execute(f"SELECT COUNT(*) FROM paper_trades p WHERE {base} AND p.trade_v2_decision='TRADE'").fetchone()[0] or 0)
    skipped=total-traded
    reasons={}
    for raw, in c.execute(f"SELECT trade_v2_skip_reasons FROM paper_trades p WHERE {base} AND p.trade_v2_decision='SKIP'").fetchall():
        try: vals=json.loads(raw or '[]')
        except Exception: vals=[]
        for x in vals: reasons[str(x)]=reasons.get(str(x),0)+1
    sizes=[(float(st or 0),int(n)) for st,n in c.execute(f"SELECT risk_stake,COUNT(*) FROM paper_trades p WHERE {base} AND p.trade_v2_decision='TRADE' GROUP BY risk_stake ORDER BY risk_stake").fetchall()]
    cost=float(getattr(config,"PAPER_TRADING_COST_PERCENT",1.0))
    def grouped(field_expr):
        rows=c.execute(f"""SELECT {field_expr},p.entry_price,p.alert_type,p.trade_side,p.risk_stake,o.price
            FROM paper_trades p JOIN signal_outcomes o ON o.signal_id=p.signal_id
            WHERE o.checkpoint_minutes=1440 AND o.status IS NOT NULL AND {base} AND p.trade_v2_decision='TRADE'""").fetchall()
        acc={}
        for key,entry,atype,side,stake,exitp in rows:
            k=str(key); st=float(stake or 0); m=_trade_math(st,float(entry),float(exitp),str(side or _side(atype)),cost)
            a=acc.setdefault(k,[0,0.0,0.0]); a[0]+=1; a[1]+=m["net_pnl"]; a[2]+=st
        return [(k,v[0],v[1],(v[1]/v[2]*100 if v[2] else None)) for k,v in acc.items()]
    sides=grouped("COALESCE(p.trade_side,'?')")
    quality=grouped("CASE WHEN p.entry_quality<55 THEN '<55' WHEN p.entry_quality<65 THEN '55-64' WHEN p.entry_quality<75 THEN '65-74' ELSE '75+' END")
    chase=grouped("CASE WHEN p.chase_risk<30 THEN '<30' WHEN p.chase_risk<50 THEN '30-49' WHEN p.chase_risk<70 THEN '50-69' ELSE '70+' END")
    return {"total":total,"traded":traded,"skipped":skipped,"reasons":reasons,"sizes":sizes,"sides":sides,"quality":quality,"chase":chase}


def _dynamic_exit_v2_stats(c)->dict[str,Any]:
    """Replay checkpoints sequentially; first rule hit closes the Paper trade.

    This uses only information available at each checkpoint, so it does not pick
    the best future exit. v2 is intentionally simple until enough samples exist.
    """
    if not bool(getattr(config,"DYNAMIC_EXIT_V2_MODE",True)):
        return {"n":0,"pnl":0.0,"roi":None,"win_rate":None,"profit_factor":None,"invested":0.0,"reasons":{}}
    rows=c.execute("""SELECT signal_id,entry_price,alert_type,trade_side,risk_stake,market_regime,
        COALESCE(news_status,'UNKNOWN'),COALESCE(news_score,0)
        FROM paper_trades WHERE trade_intelligence_version='v2' AND trade_v2_decision='TRADE'""").fetchall()
    cost=float(getattr(config,"PAPER_TRADING_COST_PERCENT",1.0)); tp=float(getattr(config,"DYNAMIC_EXIT_TAKE_PROFIT_PERCENT",4.0)); sl=float(getattr(config,"DYNAMIC_EXIT_STOP_LOSS_PERCENT",-4.0)); maxhold=int(getattr(config,"DYNAMIC_EXIT_MAX_HOLD_MINUTES",360))
    pnls=[]; invested=0.0; reasons={}
    for sid,entry,atype,side,stake,regime,news_status,news_score in rows:
        cps=c.execute("SELECT checkpoint_minutes,price FROM signal_outcomes WHERE signal_id=? AND status IS NOT NULL ORDER BY checkpoint_minutes",(sid,)).fetchall()
        chosen=None; why=None
        for minute,exitp in cps:
            minute=int(minute)
            if minute not in (60,180,360,720,1440): continue
            m=_trade_math(float(stake or 0),float(entry),float(exitp),str(side or _side(atype)),cost)
            roi=m["roi"]
            if roi>=tp: chosen=m; why="TAKE_PROFIT"; break
            if roi<=sl: chosen=m; why="STOP_LOSS"; break
            if str(news_status)=="CONTRADICTED" and float(news_score or 0)>=30: chosen=m; why="NEWS_REVERSAL"; break
            if str(regime)=="EVENT_SHOCK" and minute>=60: chosen=m; why="REGIME_EXIT"; break
            if minute>=maxhold: chosen=m; why="TIME_EXIT"; break
        if chosen is None: continue
        pnls.append(chosen["net_pnl"]); invested+=float(stake or 0); reasons[why]=reasons.get(why,0)+1
    gp=sum(x for x in pnls if x>0); gl=-sum(x for x in pnls if x<0)
    return {"n":len(pnls),"pnl":sum(pnls),"roi":sum(pnls)/invested*100 if invested else None,"win_rate":sum(x>0 for x in pnls)/len(pnls)*100 if pnls else None,"profit_factor":gp/gl if gl>0 else (float("inf") if pnls and gp>0 else None),"invested":invested,"reasons":reasons}

def get_paper_report()->dict[str,Any]:
    ensure_paper_schema()
    with closing(get_connection()) as c:
        total=int(c.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] or 0)
        checkpoints={label:_stats_from_rows(_rows(c,cp)) for cp,label in CHECKPOINTS}
        risk_checkpoints={label:_risk_stats(c,cp) for cp,label in CHECKPOINTS}
        trade_v2_checkpoints={label:_trade_v2_stats(c,cp) for cp,label in CHECKPOINTS}
        trade_v2_exit=_trade_v2_exit_stats(c)
        dynamic_exit_v2=_dynamic_exit_v2_stats(c)
        trade_v2_diag=_trade_v2_diagnostics(c)
        categories=[]
        for cat,count in c.execute("SELECT category,COUNT(*) FROM paper_trades GROUP BY category ORDER BY COUNT(*) DESC").fetchall():
            st=_stats_from_rows(_rows(c,1440,"AND p.category=?",(cat,)))
            categories.append((str(cat),int(count),st["n"],st["pnl"],st["roi"]))
        regimes=[]
        for reg,count in c.execute("SELECT COALESCE(market_regime,'LEGACY'),COUNT(*) FROM paper_trades GROUP BY COALESCE(market_regime,'LEGACY') ORDER BY COUNT(*) DESC").fetchall():
            st=_stats_from_rows(_rows(c,1440,"AND COALESCE(p.market_regime,'LEGACY')=?",(reg,)))
            regimes.append((str(reg),int(count),st["n"],st["pnl"],st["roi"]))
    return {"total":total,"open":max(0,total-checkpoints["24ч"]["n"]),"checkpoints":checkpoints,"categories":categories,"regimes":regimes,
            "risk_checkpoints":risk_checkpoints,"trade_v2_checkpoints":trade_v2_checkpoints,"trade_v2_exit":trade_v2_exit,"dynamic_exit_v2":dynamic_exit_v2,"trade_v2_diag":trade_v2_diag,"stake":float(getattr(config,"PAPER_TRADE_STAKE_USD",100)),"bank":float(getattr(config,"PAPER_TRADING_BANK_USD",10000)),"cost":float(getattr(config,"PAPER_TRADING_COST_PERCENT",1))}

def get_paper_audit(limit:int=10, checkpoint:int=1440)->list[dict[str,Any]]:
    ensure_paper_schema(); cost=float(getattr(config,"PAPER_TRADING_COST_PERCENT",1.0))
    with closing(get_connection()) as c:
        rows=c.execute("""SELECT p.signal_id,p.stake,p.entry_price,p.alert_type,p.trade_side,p.category,p.title,p.market_regime,
            o.price,o.return_percent,o.directional_return_percent,o.status,o.measured_at
            FROM paper_trades p JOIN signal_outcomes o ON o.signal_id=p.signal_id
            WHERE o.checkpoint_minutes=? AND o.status IS NOT NULL ORDER BY o.measured_at DESC LIMIT ?""",(checkpoint,limit)).fetchall()
    out=[]
    for r in rows:
        side=str(r[4] or _side(r[3])); m=_trade_math(float(r[1]),float(r[2]),float(r[8]),side,cost)
        out.append({"signal_id":r[0],"stake":r[1],"entry_yes":r[2],"alert_type":r[3],"side":side,"category":r[5],"title":r[6],
                    "regime":r[7] or "LEGACY","exit_yes":r[8],"memory_return":r[9],"directional_return":r[10],"status":r[11],"measured_at":r[12],**m})
    return out

def get_trade_v2_audit(limit:int=12)->list[dict[str,Any]]:
    """Detailed future-only audit for Trade Intelligence v2.

    TRADE rows use the frozen v2 stake. SKIP rows are evaluated hypothetically
    with the same fixed $100 benchmark stake, so we can measure whether SKIP
    actually avoided losses without changing any historical decision.
    """
    ensure_paper_schema(); cost=float(getattr(config,"PAPER_TRADING_COST_PERCENT",1.0))
    benchmark=float(getattr(config,"PAPER_TRADE_STAKE_USD",100.0))
    with closing(get_connection()) as c:
        trades=c.execute("""SELECT signal_id,opened_at,entry_price,alert_type,trade_side,category,title,
            COALESCE(market_regime,'LEGACY'),final_signal,ev_estimate,risk_score,risk_stake,
            entry_quality,chase_risk,trade_v2_decision,trade_v2_skip_reasons,trade_v2_exit_minutes,
            COALESCE(news_status,'UNKNOWN'),COALESCE(news_score,0),COALESCE(news_direction,'NEUTRAL'),news_freshest_hours,COALESCE(news_source_count,0),COALESCE(social_mentions,0)
            FROM paper_trades WHERE trade_intelligence_version='v2'
            ORDER BY opened_at DESC LIMIT ?""",(limit,)).fetchall()
        out=[]
        for r in trades:
            outcomes={int(cp):(float(price),status) for cp,price,status in c.execute(
                "SELECT checkpoint_minutes,price,status FROM signal_outcomes WHERE signal_id=? AND status IS NOT NULL",(r[0],)).fetchall()}
            side=str(r[4] or _side(r[3])); decision=str(r[14] or '?')
            stake=float(r[11] or 0) if decision=='TRADE' else benchmark
            cps={}
            for cp,label in CHECKPOINTS:
                if cp in outcomes:
                    price,status=outcomes[cp]; m=_trade_math(stake,float(r[2]),price,side,cost)
                    cps[label]={"price":price,"status":status,**m}
            chosen=int(r[16] or 360); chosen_label=next((label for cp,label in CHECKPOINTS if cp==chosen),f"{chosen}m")
            try: reasons=json.loads(r[15] or '[]')
            except Exception: reasons=[]
            out.append({"signal_id":r[0],"opened_at":r[1],"entry_yes":float(r[2]),"alert_type":r[3],"side":side,
                "category":r[5],"title":r[6],"regime":r[7],"final_signal":r[8],"ev":r[9],"risk":r[10],
                "stake":stake,"actual_stake":float(r[11] or 0),"entry_quality":float(r[12] or 0),"chase":float(r[13] or 0),
                "decision":decision,"skip_reasons":reasons,"exit_minutes":chosen,"exit_label":chosen_label,"checkpoints":cps,
                "news_status":r[17],"news_score":float(r[18] or 0),"news_direction":r[19],"news_freshest_hours":r[20],"news_source_count":int(r[21] or 0),"social_mentions":int(r[22] or 0)})
    return out


def get_trade_v2_skip_report()->dict[str,Any]:
    """Counterfactual $100 PnL of v2 SKIP decisions, by checkpoint and reason."""
    ensure_paper_schema(); cost=float(getattr(config,"PAPER_TRADING_COST_PERCENT",1.0)); stake=float(getattr(config,"PAPER_TRADE_STAKE_USD",100.0))
    with closing(get_connection()) as c:
        total=int(c.execute("SELECT COUNT(*) FROM paper_trades WHERE trade_intelligence_version='v2' AND trade_v2_decision='SKIP'").fetchone()[0] or 0)
        stats={}
        for cp,label in CHECKPOINTS:
            rows=c.execute("""SELECT p.entry_price,p.alert_type,p.trade_side,o.price,p.trade_v2_skip_reasons
                FROM paper_trades p JOIN signal_outcomes o ON o.signal_id=p.signal_id
                WHERE p.trade_intelligence_version='v2' AND p.trade_v2_decision='SKIP'
                  AND o.checkpoint_minutes=? AND o.status IS NOT NULL""",(cp,)).fetchall()
            pnls=[]
            for entry,atype,side,exitp,_ in rows:
                pnls.append(_trade_math(stake,float(entry),float(exitp),str(side or _side(atype)),cost)["net_pnl"])
            gp=sum(x for x in pnls if x>0); gl=-sum(x for x in pnls if x<0); invested=stake*len(pnls)
            stats[label]={"n":len(pnls),"pnl":sum(pnls),"roi":sum(pnls)/invested*100 if invested else None,
                "win_rate":sum(x>0 for x in pnls)/len(pnls)*100 if pnls else None,
                "profit_factor":gp/gl if gl>0 else (float('inf') if pnls and gp>0 else None)}
        reason_rows=[]
        rows=c.execute("""SELECT p.entry_price,p.alert_type,p.trade_side,o.price,p.trade_v2_skip_reasons
            FROM paper_trades p JOIN signal_outcomes o ON o.signal_id=p.signal_id
            WHERE p.trade_intelligence_version='v2' AND p.trade_v2_decision='SKIP'
              AND o.checkpoint_minutes=1440 AND o.status IS NOT NULL""").fetchall()
        acc={}
        for entry,atype,side,exitp,raw in rows:
            m=_trade_math(stake,float(entry),float(exitp),str(side or _side(atype)),cost)
            try: reasons=json.loads(raw or '[]') or ['UNKNOWN']
            except Exception: reasons=['UNKNOWN']
            for reason in reasons:
                a=acc.setdefault(str(reason),[0,0.0]); a[0]+=1; a[1]+=m['net_pnl']
        for reason,(n,pnl) in sorted(acc.items(),key=lambda kv:kv[1][1]):
            reason_rows.append((reason,n,pnl,pnl/(stake*n)*100 if n else None))
    return {"total":total,"stake":stake,"checkpoints":stats,"reasons_24h":reason_rows}


def format_trade_v2_audit(items:list[dict[str,Any]], skip_report:dict[str,Any]|None=None)->str:
    lines=["🎯 Trade v2 Audit · TRADE + SKIP",""]
    if skip_report:
        lines.append(f"🛡 SKIP counterfactual · если бы входили по ${skip_report['stake']:.0f}")
        for label in ("1ч","3ч","6ч","12ч","24ч"):
            x=skip_report['checkpoints'][label]; pf='—' if x['profit_factor'] is None else ('∞' if x['profit_factor']==float('inf') else f"{x['profit_factor']:.2f}")
            lines.append(f"• {label}: n={x['n']} · avoided {_money(-x['pnl'])} · hypot.PnL {_money(x['pnl'])} · ROI {_pct(x['roi'])} · PF {pf}")
        if skip_report['reasons_24h']:
            lines.append("• 24ч причины: "+" · ".join(f"{r} n={n} hypot.ROI {_pct(roi)}" for r,n,pnl,roi in skip_report['reasons_24h'][:4]))
        lines.append("")
    if not items:return "\n".join(lines+["Пока нет Trade v2 сигналов."])
    for i,x in enumerate(items,1):
        why=(",".join(x['skip_reasons']) if x['skip_reasons'] else '—')
        lines += [f"{i}. {x['decision']} · {x['category']} · {x['side']} · {x['regime']}",str(x['title'])[:82],
            f"Entry YES {x['entry_yes']*100:.2f}¢ · Q {x['entry_quality']:.0f} · Chase {x['chase']:.0f} · Final {float(x['final_signal'] or 0):.0f} · EV {float(x['ev'] or 0):+.1f}% · Risk {float(x['risk'] or 0):.0f}",
            f"News {x.get('news_status','UNKNOWN')} · score {x.get('news_score',0):.0f} · dir {x.get('news_direction','NEUTRAL')} · sources {x.get('news_source_count',0)} · social {x.get('social_mentions',0)}",
            (f"Stake ${x['stake']:.0f} · Exit plan {x['exit_label']}" if x['decision']=='TRADE' else f"SKIP: {why} · hypot. ${x['stake']:.0f}" )]
        cpbits=[]
        for label in ("1ч","3ч","6ч","12ч","24ч"):
            m=x['checkpoints'].get(label)
            if m: cpbits.append(f"{label} {_money(m['net_pnl'])} ({m['roi']:+.1f}%)")
        lines.append("PnL: "+(" · ".join(cpbits) if cpbits else "ждём checkpoint"))
        if x['decision']=='TRADE':
            m=x['checkpoints'].get(x['exit_label'])
            lines.append("Exit actual: "+(f"{x['exit_label']} · {_money(m['net_pnl'])} · ROI {m['roi']:+.1f}%" if m else f"{x['exit_label']} · ждём"))
        lines.append("")
    return "\n".join(lines).rstrip()

def _money(v:float)->str:return f"{v:+,.2f}$"
def _pct(v:Any)->str:return "—" if v is None else f"{float(v):.1f}%"
def format_paper_report(r:dict[str,Any])->str:
    lines=["💼 Paper Trading · LIVE","",f"Виртуальный банк: ${r['bank']:,.0f}",f"Позиция: ${r['stake']:,.0f} на сигнал",f"Издержки: {r['cost']:.1f}% на сделку",
           f"Сделок открыто всего: {r['total']}",f"Ждут 24ч: {r['open']}","","⏱ Результаты (реальная сторона YES/NO)"]
    for label in ("1ч","3ч","6ч","12ч","24ч"):
        x=r["checkpoints"][label]; pf="—" if x["profit_factor"] is None else ("∞" if x["profit_factor"]==float("inf") else f"{x['profit_factor']:.2f}")
        lines.append(f"• {label}: n={x['n']} · PnL {_money(x['pnl'])} · ROI {_pct(x['roi'])} · Win {_pct(x['win_rate'])} · PF {pf}")
    lines += ["","🧠 Risk Engine sizing · те же сигналы"]
    for label in ("1ч","3ч","6ч","12ч","24ч"):
        x=r["risk_checkpoints"][label]; pf="—" if x["profit_factor"] is None else ("∞" if x["profit_factor"]==float("inf") else f"{x['profit_factor']:.2f}")
        lines.append(f"• {label}: n={x['n']} · PnL {_money(x['pnl'])} · ROI {_pct(x['roi'])} · Win {_pct(x['win_rate'])} · PF {pf} · вложено ${x['invested']:,.0f}")
    lines += ["","🎯 Trade Intelligence v2 · SKIP + sizing"]
    d=r["trade_v2_diag"]
    lines.append(f"• новых: {d['total']} · TRADE {d['traded']} · SKIP {d['skipped']}")
    if d["sizes"]: lines.append("• размеры: "+" · ".join(f"${st:.0f}×{n}" for st,n in d["sizes"]))
    if d["reasons"]: lines.append("• SKIP: "+" · ".join(f"{k}×{v}" for k,v in sorted(d["reasons"].items(),key=lambda x:-x[1])[:4]))
    if d["sides"]: lines.append("• 24ч side: "+" · ".join(f"{k} n={n} ROI {_pct(roi)}" for k,n,pnl,roi in d["sides"]))
    if d["quality"]: lines.append("• 24ч EntryQ: "+" · ".join(f"{k} n={n} ROI {_pct(roi)}" for k,n,pnl,roi in d["quality"]))
    if d["chase"]: lines.append("• 24ч Chase: "+" · ".join(f"{k} n={n} ROI {_pct(roi)}" for k,n,pnl,roi in d["chase"]))
    for label in ("1ч","3ч","6ч","12ч","24ч"):
        x=r["trade_v2_checkpoints"][label]; pf="—" if x["profit_factor"] is None else ("∞" if x["profit_factor"]==float("inf") else f"{x['profit_factor']:.2f}")
        lines.append(f"• {label}: n={x['n']} · PnL {_money(x['pnl'])} · ROI {_pct(x['roi'])} · PF {pf}")
    x=r["trade_v2_exit"]; pf="—" if x["profit_factor"] is None else ("∞" if x["profit_factor"]==float("inf") else f"{x['profit_factor']:.2f}")
    lines += ["","🚪 Exit Engine v1 · горизонт выбран при входе",f"• закрыто: n={x['n']} · PnL {_money(x['pnl'])} · ROI {_pct(x['roi'])} · Win {_pct(x['win_rate'])} · PF {pf}"]
    y=r.get("dynamic_exit_v2") or {}; ypf="—" if y.get("profit_factor") is None else ("∞" if y.get("profit_factor")==float("inf") else f"{y.get('profit_factor'):.2f}")
    lines += ["","🧭 Dynamic Exit Engine v2 · Paper/Shadow",f"• закрыто: n={y.get('n',0)} · PnL {_money(y.get('pnl',0))} · ROI {_pct(y.get('roi'))} · Win {_pct(y.get('win_rate'))} · PF {ypf}"]
    if y.get("reasons"): lines.append("• exits: "+" · ".join(f"{k}×{v}" for k,v in sorted(y['reasons'].items(),key=lambda z:-z[1])))
    lines += ["","🏷 24ч по категориям"]
    for cat,sent,n,pnl,roi in r["categories"]: lines.append(f"• {cat}: сделок {sent} · проверено {n} · PnL {_money(pnl)} · ROI {_pct(roi)}")
    lines += ["","🌊 24ч по режимам рынка"]
    for reg,sent,n,pnl,roi in r["regimes"]: lines.append(f"• {reg}: сделок {sent} · проверено {n} · PnL {_money(pnl)} · ROI {_pct(roi)}")
    lines += ["","ℹ️ PUMP моделируется покупкой YES, DIP — покупкой NO. Market Regime, News/Social, Trade v2 и Dynamic Exit работают в Shadow/Paper и не влияют на отправку."]
    return "\n".join(lines)

def format_paper_audit(items:list[dict[str,Any]])->str:
    lines=["🔎 Paper PnL Audit · последние 24ч сделки",""]
    if not items:return "\n".join(lines+["Пока нет закрытых сделок."])
    for i,x in enumerate(items,1):
        lines += [f"{i}. {x['category']} · {x['side']} · {x['regime']}",str(x['title'])[:90],
                  f"YES: {float(x['entry_yes'])*100:.2f}¢ → {float(x['exit_yes'])*100:.2f}¢ | сторона: {x['entry_side']*100:.2f}¢ → {x['exit_side']*100:.2f}¢",
                  f"Shares: {x['shares']:.2f} · Gross {_money(x['gross_pnl'])} · costs {x['costs']:.2f}$ · NET {_money(x['net_pnl'])} · ROI {x['roi']:+.1f}%",
                  f"AI Memory directional: {float(x['directional_return']):+.1f}% · {x['status']}",""]
    return "\n".join(lines).rstrip()
