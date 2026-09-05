"""News & Social Intelligence v1 — external catalyst context for final alerts.

Shadow/Paper only. Uses Google News RSS without an API key, Reddit public search,
and optionally X recent search when X_BEARER_TOKEN is configured. Network failures
are fail-open and never stop Telegram delivery.
"""
from __future__ import annotations
import concurrent.futures, re, time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote_plus
import httpx
import config

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
STOP={"will","the","a","an","by","before","after","be","is","are","to","of","in","on","at","and","or","for","from","reach","above","below","between","market","close","day","2026","2027","2028"}
POS={"approve","approved","win","wins","launch","launched","pass","passed","deal","agreement","confirmed","surge","record","support","backs","raise","raised","reaches","hits","elected"}
NEG={"reject","rejected","lose","loses","delay","delayed","deny","denied","cancel","cancelled","fails","failed","drop","falls","lawsuit","probe","ban","blocked","unlikely"}
RUMOR={"rumor","rumour","reportedly","sources say","may","could","considering","speculation"}
OFFICIAL={"official","announces","announced","statement","filing","sec","court","government","white house","company says","confirmed"}

def _tokens(title:str)->list[str]:
    return [x for x in re.findall(r"[a-zA-Z0-9$]+", title.lower()) if len(x)>2 and x not in STOP][:8]

def _query(title:str)->str:
    toks=_tokens(title)
    return " ".join(toks[:6]) or title[:120]

def _age_hours(raw:str|None)->float|None:
    if not raw: return None
    try:
        dt=parsedate_to_datetime(raw)
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        return max(0.0,(datetime.now(timezone.utc)-dt).total_seconds()/3600)
    except Exception: return None

def _google_news(q:str)->list[dict[str,Any]]:
    import xml.etree.ElementTree as ET
    url=f"https://news.google.com/rss/search?q={quote_plus(q + ' when:1d')}&hl=en-US&gl=US&ceid=US:en"
    with httpx.Client(timeout=float(getattr(config,"NEWS_HTTP_TIMEOUT_SECONDS",4.0)),headers={"User-Agent":"PolymarketSmartScanner/1.0"},follow_redirects=True) as c:
        r=c.get(url); r.raise_for_status(); root=ET.fromstring(r.text)
    out=[]
    for item in root.findall(".//item")[:12]:
        out.append({"kind":"news","title":item.findtext("title") or "","url":item.findtext("link") or "","source":item.findtext("source") or "Google News","age_h":_age_hours(item.findtext("pubDate"))})
    return out

def _reddit(q:str)->list[dict[str,Any]]:
    url=f"https://www.reddit.com/search.json?q={quote_plus(q)}&sort=new&t=day&limit=12"
    with httpx.Client(timeout=float(getattr(config,"NEWS_HTTP_TIMEOUT_SECONDS",4.0)),headers={"User-Agent":"PolymarketSmartScanner/1.0"},follow_redirects=True) as c:
        r=c.get(url); r.raise_for_status(); data=r.json()
    out=[]
    now=time.time()
    for ch in data.get("data",{}).get("children",[]):
        d=ch.get("data",{}); created=float(d.get("created_utc") or now)
        out.append({"kind":"social","title":str(d.get("title") or ""),"url":"https://www.reddit.com"+str(d.get("permalink") or ""),"source":"Reddit","age_h":max(0,(now-created)/3600),"score":int(d.get("score") or 0)})
    return out

def _x_search(q:str)->list[dict[str,Any]]:
    token=str(getattr(config,"X_BEARER_TOKEN","") or "").strip()
    if not token: return []
    url="https://api.x.com/2/tweets/search/recent"
    params={"query":q+" -is:retweet lang:en","max_results":10,"tweet.fields":"created_at,public_metrics"}
    with httpx.Client(timeout=float(getattr(config,"NEWS_HTTP_TIMEOUT_SECONDS",4.0)),headers={"Authorization":f"Bearer {token}"}) as c:
        r=c.get(url,params=params); r.raise_for_status(); data=r.json()
    out=[]
    for d in data.get("data",[]):
        age=None
        try: age=max(0,(datetime.now(timezone.utc)-datetime.fromisoformat(str(d.get("created_at")).replace("Z","+00:00"))).total_seconds()/3600)
        except Exception: pass
        out.append({"kind":"social","title":str(d.get("text") or ""),"url":"","source":"X","age_h":age,"score":int((d.get("public_metrics") or {}).get("like_count") or 0)})
    return out

def analyze_news_social(alert:dict[str,Any])->dict[str,Any]:
    if not bool(getattr(config,"NEWS_SOCIAL_INTELLIGENCE_MODE",True)):
        return {"news_status":"DISABLED","news_score":0.0}
    title=str(alert.get("title") or alert.get("question") or "").strip()
    q=_query(title); key=q.lower()
    ttl=int(getattr(config,"NEWS_CACHE_SECONDS",900)); cached=_CACHE.get(key)
    if cached and time.time()-cached[0] < ttl: return dict(cached[1])
    items=[]; errors=[]
    funcs=[("google",_google_news), ("reddit",_reddit)]
    if str(getattr(config,"X_BEARER_TOKEN","") or "").strip(): funcs.append(("x",_x_search))
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(funcs)) as ex:
        futs={ex.submit(fn,q):name for name,fn in funcs}
        for f,name in [(f,n) for f,n in futs.items()]:
            try: items.extend(f.result())
            except Exception as e: errors.append(name)
    toks=set(_tokens(title)); relevant=[]
    for x in items:
        text=str(x.get("title") or "").lower(); hit=sum(1 for t in toks if t in text)
        rel=hit/max(1,min(4,len(toks)))
        if hit>=2 or rel>=0.5:
            x["relevance"]=min(100.0,rel*100); relevant.append(x)
    relevant.sort(key=lambda x: ((x.get("age_h") if x.get("age_h") is not None else 999),-float(x.get("relevance",0))))
    texts=" ".join(str(x.get("title") or "").lower() for x in relevant[:10])
    pos=sum(texts.count(w) for w in POS); neg=sum(texts.count(w) for w in NEG)
    direction="YES" if pos>neg else ("NO" if neg>pos else "NEUTRAL")
    freshest=min([x["age_h"] for x in relevant if x.get("age_h") is not None],default=None)
    news=[x for x in relevant if x.get("kind")=="news"]; social=[x for x in relevant if x.get("kind")=="social"]
    sources=len(set(str(x.get("source")) for x in news)); official=any(w in texts for w in OFFICIAL); rumor=any(w in texts for w in RUMOR)
    if not relevant: status="NO_CATALYST"
    elif rumor and not official: status="RUMOR"
    else: status="CONFIRMED_NEWS" if (official or sources>=2) else "RUMOR"
    expected="NO" if any(k in str(alert.get("alert_type") or "").upper() for k in ("DIP","DROP","BEAR")) else "YES"
    if direction in ("YES","NO") and direction != expected and (official or sources>=2): status="CONTRADICTED"
    score=min(100.0, len(news)*9 + len(social)*4 + sources*8 + (15 if official else 0) + (10 if freshest is not None and freshest<=2 else 0))
    result={"news_status":status,"news_score":round(score,1),"news_direction":direction,"news_relevance":round(sum(float(x.get("relevance",0)) for x in relevant[:5])/max(1,min(5,len(relevant))),1),"news_freshest_hours":None if freshest is None else round(freshest,2),"news_source_count":sources,"news_items_count":len(relevant),"social_mentions":len(social),"news_query":q,"news_top_items":relevant[:5],"news_errors":errors}
    _CACHE[key]=(time.time(),result); return dict(result)

def enrich_with_news_social(alert:dict[str,Any])->dict[str,Any]:
    try: return {**alert,**analyze_news_social(alert)}
    except Exception: return {**alert,"news_status":"ERROR","news_score":0.0,"news_direction":"NEUTRAL","news_items_count":0,"social_mentions":0}
