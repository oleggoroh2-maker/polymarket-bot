"""Category Classification v2 + historical audit.

Fixes substring collisions such as ETH inside "Elizabeth". Historical rows are
never rewritten; the audit only compares stored category with title-based v2.
"""
from __future__ import annotations
import re
from collections import Counter
from contextlib import closing
from typing import Any
from database import get_connection

CATEGORY_VERSION = "v2-word-boundary"

_PATTERNS = [
    ("₿ CRYPTO", [r"\bbitcoin\b", r"\bbtc\b", r"\bethereum\b", r"\beth\b", r"\bsolana\b", r"\bxrp\b", r"\bdogecoin\b", r"\bdoge\b", r"\bcrypto(?:currency)?\b"]),
    ("🤖 AI/TECH", [r"\bopenai\b", r"\banthropic\b", r"\bchatgpt\b", r"\bnvidia\b", r"\bspacex\b", r"\bartificial intelligence\b", r"\bai\b"]),
    ("📈 ETF", [r"\betf\b", r"\bexchange[- ]traded fund\b", r"\bblackrock\b", r"\bfidelity\b"]),
    ("🏛 POLITICS", [r"\belection\b", r"\bpresident(?:ial)?\b", r"\bdemocrat(?:ic)?\b", r"\brepublican\b", r"\bsenate\b", r"\bsenator\b", r"\bgovernor\b", r"\bcongress\b", r"\bprime minister\b", r"\btrump\b", r"\bzelenskyy\b", r"\bputin\b"]),
    ("⚽ SPORTS", [r"\bnba\b", r"\bnfl\b", r"\bnhl\b", r"\bmlb\b", r"\bf1\b", r"\bformula 1\b", r"\bfootball\b", r"\bsoccer\b", r"\bmls\b", r"\bchampion(?:ship)?\b", r"\bworld cup\b", r"\bballon d.or\b"]),
]
_COMPILED=[(cat,[re.compile(p,re.I) for p in pats]) for cat,pats in _PATTERNS]

def classify_category(title: str) -> str:
    text=" ".join(str(title or "").split())
    for category, patterns in _COMPILED:
        if any(p.search(text) for p in patterns):
            return category
    return "📦 OTHER"

def _canon(value: Any) -> str:
    x=str(value or "").upper()
    for key in ("CRYPTO","AI/TECH","ETF","POLITICS","SPORTS","OTHER"):
        if key in x: return key
    if "AI" in x or "TECH" in x: return "AI/TECH"
    return "OTHER"

def get_category_audit(limit: int=5000) -> dict[str,Any]:
    with closing(get_connection()) as c:
        rows=c.execute("SELECT title,category FROM ai_signals ORDER BY created_at DESC LIMIT ?",(max(1,int(limit)),)).fetchall()
    mismatches=[]; transitions=Counter()
    for title,stored in rows:
        predicted=classify_category(str(title or ""))
        if _canon(stored)!=_canon(predicted):
            transitions[(str(stored or "OTHER"),predicted)]+=1
            if len(mismatches)<10: mismatches.append((str(title or ""),str(stored or "OTHER"),predicted))
    return {"n":len(rows),"mismatches":sum(transitions.values()),"transitions":transitions.most_common(8),"examples":mismatches,"version":CATEGORY_VERSION}

def format_category_audit(r:dict[str,Any])->str:
    n=int(r.get("n") or 0); m=int(r.get("mismatches") or 0); pct=(m/n*100 if n else 0)
    lines=["🏷 Category Classification v2 · Audit",f"Проверено: {n} · расхождений: {m} ({pct:.1f}%)",f"Версия: {r.get('version')}","","Частые исправления (история не переписывается):"]
    for (old,new),count in r.get("transitions") or []: lines.append(f"• {old} → {new}: {count}")
    if r.get("examples"):
        lines += ["","Примеры:"]
        for title,old,new in r["examples"][:6]: lines.append(f"• {title[:70]}\n  {old} → {new}")
    lines += ["","ℹ️ Новые сканы используют word-boundary классификацию; старые AI Memory/Paper записи не изменяются."]
    return "\n".join(lines)
