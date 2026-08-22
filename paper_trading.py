"""Paper Trading v1 — virtual PnL for alerts that were actually delivered.

This module never changes alert selection or sends real orders. A trade is
opened once per ai_signal_id after the first successful Telegram delivery and
is evaluated from the existing AI Memory checkpoints.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import config
from database import get_connection

CHECKPOINTS = ((60, "1ч"), (360, "6ч"), (1440, "24ч"))


def ensure_paper_schema() -> None:
    with closing(get_connection()) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                signal_id TEXT PRIMARY KEY,
                opened_at TEXT NOT NULL,
                stake REAL NOT NULL,
                entry_price REAL NOT NULL,
                category TEXT,
                alert_type TEXT,
                title TEXT,
                final_signal REAL,
                ev_estimate REAL,
                risk_score REAL
            );
            CREATE INDEX IF NOT EXISTS idx_paper_trades_opened
            ON paper_trades(opened_at);
            """
        )
        c.commit()


def record_delivered_trade(alert: dict[str, Any]) -> bool:
    """Open one virtual trade after successful Telegram delivery."""
    if not bool(getattr(config, "PAPER_TRADING_MODE", True)):
        return False
    signal_id = str(alert.get("ai_signal_id") or "").strip()
    if not signal_id:
        return False
    ensure_paper_schema()
    stake = float(getattr(config, "PAPER_TRADE_STAKE_USD", 100.0))
    entry = float(alert.get("current_price", alert.get("price")) or 0.0)
    with closing(get_connection()) as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO paper_trades
            (signal_id, opened_at, stake, entry_price, category, alert_type,
             title, final_signal, ev_estimate, risk_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_id, datetime.now(timezone.utc).isoformat(), stake, entry,
                str(alert.get("category") or "OTHER"), str(alert.get("alert_type") or ""),
                str(alert.get("title") or ""), float(alert.get("final_signal_score") or 0),
                float(alert.get("ev_estimate_percent") or 0), float(alert.get("risk_score") or 0),
            ),
        )
        c.commit()
        return cur.rowcount > 0


def _checkpoint_stats(c, minutes: int) -> dict[str, Any]:
    rows = c.execute(
        """SELECT p.stake, o.directional_return_percent
        FROM paper_trades p JOIN signal_outcomes o ON o.signal_id=p.signal_id
        WHERE o.checkpoint_minutes=? AND o.status IS NOT NULL""", (minutes,)
    ).fetchall()
    cost_pct = float(getattr(config, "PAPER_TRADING_COST_PERCENT", 1.0))
    raw = [float(stake) * float(ret) / 100.0 for stake, ret in rows]
    realistic = [x - float(stake) * cost_pct / 100.0 for x, (stake, _ret) in zip(raw, rows)]
    wins = sum(1 for x in realistic if x > 0)
    gross_profit = sum(x for x in realistic if x > 0)
    gross_loss = -sum(x for x in realistic if x < 0)
    invested = sum(float(r[0]) for r in rows)
    return {
        "n": len(rows), "raw_pnl": sum(raw), "pnl": sum(realistic),
        "roi": (sum(realistic) / invested * 100.0) if invested else None,
        "win_rate": (wins / len(rows) * 100.0) if rows else None,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else (None if not rows else float("inf")),
    }


def get_paper_report() -> dict[str, Any]:
    ensure_paper_schema()
    with closing(get_connection()) as c:
        total = int(c.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] or 0)
        checkpoints = {label: _checkpoint_stats(c, cp) for cp, label in CHECKPOINTS}
        closed24 = checkpoints["24ч"]["n"]
        open_count = max(0, total - closed24)
        categories = []
        cost_pct = float(getattr(config, "PAPER_TRADING_COST_PERCENT", 1.0))
        for cat, count in c.execute("SELECT category,COUNT(*) FROM paper_trades GROUP BY category ORDER BY COUNT(*) DESC").fetchall():
            rows = c.execute(
                """SELECT p.stake,o.directional_return_percent FROM paper_trades p
                JOIN signal_outcomes o ON o.signal_id=p.signal_id
                WHERE p.category=? AND o.checkpoint_minutes=1440 AND o.status IS NOT NULL""", (cat,)
            ).fetchall()
            pnl = sum(float(s) * (float(r) - cost_pct) / 100.0 for s, r in rows)
            invested = sum(float(s) for s, _ in rows)
            categories.append((str(cat), int(count), len(rows), pnl, pnl / invested * 100.0 if invested else None))
    return {"total": total, "open": open_count, "checkpoints": checkpoints, "categories": categories,
            "stake": float(getattr(config, "PAPER_TRADE_STAKE_USD", 100.0)),
            "bank": float(getattr(config, "PAPER_TRADING_BANK_USD", 10000.0)),
            "cost": float(getattr(config, "PAPER_TRADING_COST_PERCENT", 1.0))}


def _money(v: float) -> str: return f"{v:+,.2f}$"
def _pct(v: Any) -> str: return "—" if v is None else f"{float(v):.1f}%"

def format_paper_report(r: dict[str, Any]) -> str:
    lines = ["💼 Paper Trading · LIVE", "",
             f"Виртуальный банк: ${r['bank']:,.0f}", f"Позиция: ${r['stake']:,.0f} на сигнал",
             f"Издержки: {r['cost']:.1f}% на сделку", f"Сделок открыто всего: {r['total']}",
             f"Ждут 24ч: {r['open']}", "", "⏱ Результаты"]
    for label in ("1ч", "6ч", "24ч"):
        x = r["checkpoints"][label]
        pf = "—" if x["profit_factor"] is None else ("∞" if x["profit_factor"] == float("inf") else f"{x['profit_factor']:.2f}")
        lines.append(f"• {label}: n={x['n']} · PnL {_money(x['pnl'])} · ROI {_pct(x['roi'])} · Win {_pct(x['win_rate'])} · PF {pf}")
    lines += ["", "🏷 24ч по категориям"]
    if not r["categories"]: lines.append("• пока нет данных")
    for cat, sent, n, pnl, roi in r["categories"]:
        lines.append(f"• {cat}: сделок {sent} · проверено {n} · PnL {_money(pnl)} · ROI {_pct(roi)}")
    lines += ["", "ℹ️ Paper Trading не совершает реальных сделок и не влияет на фильтры. PnL считается по существующим контрольным замерам AI Memory; из результата вычитаются заданные виртуальные издержки."]
    return "\n".join(lines)
