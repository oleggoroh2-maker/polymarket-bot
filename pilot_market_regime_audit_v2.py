"""Read-only Pilot v2 market-context diagnostics; no reconstructed regime labels."""
import re
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from statistics import median
from database import get_connection
from pilot_engine_v2 import VERSION

PATTERNS = (
 ('CRYPTO', r'\b(bitcoin|btc|ethereum|eth|solana|sol|crypto|token|defi|blockchain|dogecoin|xrp|sui|airdrop|stablecoin|usdt|coinbase|binance|kraken|polymarket)\b'),
 ('SPORTS', r'\b(nfl|nba|nhl|mlb|super bowl|world cup|champions league|premier league|ufc|f1|formula 1|football|soccer|tennis|viking[s]?|lions|playoffs|win the game|win the match)\b'),
 ('POLITICS', r'\b(election|electoral|president|prime minister|parliament|senate|congress|governor|mayor|ballot|nomination|referendum|political party|democrat|republican|labour party|snap election|popular vote)\b'),
 ('AI_TECH', r'\b(openai|chatgpt|gemini|gpt|anthropic|claude|artificial intelligence|ai model|llm|apple|google|nvidia|tesla|spacex|software|iphone|arena leaderboard)\b'),
 ('FINANCE_ECONOMY', r'\b(stock|stocks|s&p|nasdaq|dow jones|inflation|interest rate|fed rate|federal reserve|gdp|cpi|recession|ipo|valuation|market cap|earnings|unemployment|treasury)\b'),
 ('ENTERTAINMENT', r'\b(album|song|movie|film|oscar|grammy|netflix|celebrity|taylor swift|box office|billboard|music)\b'),
 ('GEOPOLITICS', r'\b(war|ceasefire|invasion|capture|ukraine|russia|iran|israel|gaza|sanction|territory)\b'),
)

def category(title, raw):
    t=str(title or '').lower()
    for name,pattern in PATTERNS:
        if re.search(pattern,t,re.I):return name
    c=str(raw or '').upper().strip()
    return 'OTHER_UNCLASSIFIED' if c in ('','OTHER','UNKNOWN') else 'SOURCE_'+c[:45]

def stats(items):
    if not items:return dict(n=0,roi=None,pf=None,win=None,median=None,pnl=0)
    r=[float(x[1]) for x in items]; p=[float(x[0])*float(x[1])/100 for x in items]
    gp=sum(max(x,0) for x in p); gl=-sum(min(x,0) for x in p)
    return dict(n=len(items),roi=sum(r)/len(r),pf=(gp/gl if gl else ('Infinity' if gp else None)),win=100*sum(x>0 for x in r)/len(r),median=median(r),pnl=sum(p))

def move(v):
    if v is None:return 'Unknown'
    try:
        x=abs(float(v))
        return '<5%' if x<5 else '5–15%' if x<15 else '15–30%' if x<30 else '≥30%'
    except (ValueError,TypeError):return 'Unknown'

def report():
    with closing(get_connection()) as c:
        rows=c.execute('''SELECT d.candidate_id,d.decided_at,d.stake,e.title,e.category,e.change_1h,e.change_24h,o.roi
          FROM pilot_engine_v2_decisions d JOIN entry_discovery_candidates e ON e.id=d.candidate_id
          LEFT JOIN entry_discovery_outcomes o ON o.candidate_id=d.candidate_id AND o.checkpoint_minutes=1440
          WHERE d.version=? AND d.action='TRADE' ORDER BY d.decided_at,d.candidate_id''',(VERSION,)).fetchall()
    buckets={k:defaultdict(list) for k in ('category','period','move_1h','move_24h','category_x_move_24h')}
    for cid,t,stake,title,raw,c1,c24,roi in rows:
        if roi is None:continue
        try:day=datetime.fromisoformat(str(t).replace('Z','+00:00')).astimezone(timezone.utc).strftime('%Y-%m-%d')
        except (ValueError,TypeError):day='Unknown'
        cat=category(title,raw); m24=move(c24)
        for k,v in {'category':cat,'period':day,'move_1h':move(c1),'move_24h':m24,'category_x_move_24h':cat+' / '+m24}.items():
            buckets[k][v].append((stake,roi))
    sections={k:[dict(label=label,**stats(v)) for label,v in sorted(b.items(),key=lambda x:(-len(x[1]),x[0]))] for k,b in buckets.items()}
    robustness=[]
    for k in ('category','move_24h','category_x_move_24h'):
        for label,items in buckets[k].items():
            if len(items)<8:continue
            ordered=sorted(items,key=lambda x:x[1]); no_best=ordered[:-1]; no_worst=ordered[1:]; no_both=ordered[1:-1]
            robustness.append(dict(dimension=k,label=label,n=len(items),normal=stats(items)['roi'],without_best=stats(no_best)['roi'],without_worst=stats(no_worst)['roi'],without_both=stats(no_both)['roi']))
    robustness.sort(key=lambda x:(x['dimension'],-x['n'],x['label']))
    return dict(mode='READ_ONLY',total_trades=len(rows),matured=sum(x[-1] is not None for x in rows),sections=sections,robustness=robustness,
       note='Heuristic title-based categories are provisional and may overlap; first matching keyword wins. 1h/24h entry changes describe the candidate, not market-wide volatility. No independent BTC/equity regime history is available here; no correction/manipulation claims. All groups are exploratory, subject to selection bias and small samples. PAPER outcomes omit execution costs and slippage. No trading logic, database writes or Telegram changes.')
