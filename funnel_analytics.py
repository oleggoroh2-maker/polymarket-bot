"""Persistent scan-funnel telemetry. Analytics only."""
from __future__ import annotations
from contextlib import closing
from datetime import datetime,timezone,timedelta
from database import get_connection

def ensure_schema():
    with closing(get_connection()) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS funnel_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, markets INTEGER, liquidity_pass INTEGER, detected INTEGER, grouped INTEGER, cooldown_pass INTEGER, memory_recorded INTEGER, quality_pass INTEGER, trade_route INTEGER, watch_route INTEGER, move_route INTEGER, dropped_route INTEGER)'''); c.commit()

def record_funnel(d):
    ensure_schema()
    keys=['markets','liquidity_pass','detected','grouped','cooldown_pass','memory_recorded','quality_pass','trade_route','watch_route','move_route','dropped_route']
    with closing(get_connection()) as c:
        c.execute('INSERT INTO funnel_snapshots(created_at,'+','.join(keys)+') VALUES (?,'+','.join('?' for _ in keys)+')',(datetime.now(timezone.utc).isoformat(),*[int(d.get(k,0)) for k in keys])); c.commit()

def format_funnel(hours=24):
    ensure_schema(); since=(datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
    with closing(get_connection()) as c:
        r=c.execute('''SELECT COUNT(*),COALESCE(SUM(markets),0),COALESCE(SUM(liquidity_pass),0),COALESCE(SUM(detected),0),COALESCE(SUM(grouped),0),COALESCE(SUM(cooldown_pass),0),COALESCE(SUM(memory_recorded),0),COALESCE(SUM(quality_pass),0),COALESCE(SUM(trade_route),0),COALESCE(SUM(watch_route),0),COALESCE(SUM(move_route),0),COALESCE(SUM(dropped_route),0) FROM funnel_snapshots WHERE created_at>=?''',(since,)).fetchone()
    return (f'🔬 Signal Funnel · последние {hours}ч\n\nСканов: {r[0]}\nРынков просмотрено: {r[1]}\nПосле liquidity: {r[2]}\nСырых alert-кандидатов: {r[3]}\nПосле группировки: {r[4]}\nПосле cooldown: {r[5]}\nЗаписано в AI Memory: {r[6]}\nQuality v3 PASS: {r[7]}\n\n📨 Routing\n🔥 TRADE: {r[8]}\n🟡 WATCH: {r[9]}\n👀 MARKET MOVE: {r[10]}\nОтброшено routing: {r[11]}\n\nℹ️ Это диагностическая воронка, она не меняет фильтры.')
