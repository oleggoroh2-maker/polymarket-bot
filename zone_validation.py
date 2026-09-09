"""Walk-forward validation and decomposition for historical Positive Zones.
Diagnostic/shadow only. Never changes live routing or scores.
"""
from __future__ import annotations
from collections import defaultdict
from outcome_recalibration_v2 import _rows, _dimensions, _stat

MIN_DISCOVERY = 35
MIN_VALIDATION = 15
TOP_DISCOVERY = 12
CORE_ZONES = (
    ("Similarity", "70–79", "Category", "CRYPTO"),
    ("Volume Δ", "300+", "Category", "CRYPTO"),
)

def _match(row, dims, a, va, b, vb):
    return dims[a](row) == va and dims[b](row) == vb

def _fmt_stat(items):
    if not items:
        return {"n":0,"adj":None,"avg":None,"strong":None}
    return _stat(items)

def get_walk_forward_validation(minutes:int=1440, limit:int=10000):
    rows=_rows(minutes,limit)
    rows.sort(key=lambda r:r.get('created_at',''))
    n=len(rows); a=int(n*.60); b=int(n*.80)
    parts={"DISCOVERY":rows[:a],"VALIDATION":rows[a:b],"HOLDOUT":rows[b:]}
    dims=_dimensions(); names=list(dims); found=[]
    for i,x in enumerate(names):
        for y in names[i+1:]:
            groups=defaultdict(list)
            for r in parts['DISCOVERY']:
                groups[(dims[x](r),dims[y](r))].append(r)
            for (vx,vy),items in groups.items():
                if len(items)<MIN_DISCOVERY: continue
                st=_stat(items)
                if st['adj']>0 and st['strong']>=15:
                    found.append((st['adj'],st['n'],x,vx,y,vy))
    found.sort(reverse=True)
    zones=[]
    for _,__,x,vx,y,vy in found[:TOP_DISCOVERY]:
        z={"a":x,"va":vx,"b":y,"vb":vy,"parts":{}}
        for pn,pr in parts.items():
            items=[r for r in pr if _match(r,dims,x,vx,y,vy)]
            z['parts'][pn]=_fmt_stat(items)
        val=z['parts']['VALIDATION']; hold=z['parts']['HOLDOUT']
        z['stable']=(val['n']>=MIN_VALIDATION and hold['n']>=MIN_VALIDATION and val['adj'] is not None and hold['adj'] is not None and val['adj']>0 and hold['adj']>0)
        zones.append(z)
    return {"minutes":minutes,"n":n,"sizes":{k:len(v) for k,v in parts.items()},"zones":zones,"category_mode":"v2 title reclassification"}

def get_zone_decomposition(minutes:int=1440, limit:int=10000):
    rows=_rows(minutes,limit); dims=_dimensions(); out=[]
    for a,va,b,vb in CORE_ZONES:
        base=[r for r in rows if _match(r,dims,a,va,b,vb)]
        breakdown=[]
        for name in ('Direction','Price','Volume Δ','Liquidity Δ','AI Risk','Score'):
            if name in (a,b): continue
            groups=defaultdict(list)
            for r in base: groups[dims[name](r)].append(r)
            vals=[]
            for label,items in groups.items():
                if len(items)>=15: vals.append((label,_stat(items)))
            vals.sort(key=lambda x:x[1]['adj'],reverse=True)
            if vals: breakdown.append({'name':name,'best':vals[0],'worst':vals[-1]})
        out.append({'a':a,'va':va,'b':b,'vb':vb,'base':_fmt_stat(base),'breakdown':breakdown})
    return {'minutes':minutes,'n':len(rows),'zones':out}

def _pct(x): return '—' if x is None else f"{x:+.1f}%"

def format_walk_forward_validation(r):
    h=int(r['minutes']/60); s=r['sizes']
    lines=[f"🧪 Zone Walk-Forward · {h}ч",f"База {r['n']} · chronological 60/20/20 · Category v2",f"Discovery {s['DISCOVERY']} · Validation {s['VALIDATION']} · Holdout {s['HOLDOUT']}",""]
    if not r['zones']: lines.append('Discovery не нашёл положительных зон с достаточным n.')
    for z in r['zones']:
        d=z['parts']['DISCOVERY']; v=z['parts']['VALIDATION']; hld=z['parts']['HOLDOUT']
        mark='✅ STABLE' if z['stable'] else '⚠️ UNCONFIRMED'
        lines.append(f"• {z['a']}={z['va']} × {z['b']}={z['vb']} · {mark}")
        lines.append(f"  D n={d['n']} adj {_pct(d['adj'])} · V n={v['n']} adj {_pct(v['adj'])} · H n={hld['n']} adj {_pct(hld['adj'])}")
    lines += ["","ℹ️ Зона считается STABLE только если Validation и Holdout имеют n≥15 и положительный adj. Никаких live-изменений."]
    return '\n'.join(lines)

def format_zone_decomposition(r):
    lines=[f"🔬 Zone Decomposition · {int(r['minutes']/60)}ч","Проверка ядра двух главных historical zones",""]
    for z in r['zones']:
        bs=z['base']; lines.append(f"• {z['a']}={z['va']} × {z['b']}={z['vb']} · n={bs['n']} adj {_pct(bs['adj'])}")
        for x in z['breakdown']:
            bl,bst=x['best']; wl,wst=x['worst']
            lines.append(f"  {x['name']}: 🟢 {bl} n={bst['n']} {_pct(bst['adj'])} · 🔴 {wl} n={wst['n']} {_pct(wst['adj'])}")
    lines += ["","ℹ️ Decomposition ищет, какая третья переменная усиливает или разрушает зону. Shadow analytics only."]
    return '\n'.join(lines)
