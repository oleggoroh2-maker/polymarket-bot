"""Positive Zone Finder — discovers historical two-factor contexts with positive outcomes."""
from __future__ import annotations
from collections import defaultdict
from outcome_recalibration_v2 import _rows,_dimensions,_stat

MIN_ZONE_SAMPLES=35

def get_positive_zones(minutes:int=180,limit:int=10000,max_zones:int=12):
    rows=_rows(minutes,limit); dims=_dimensions(); names=list(dims); zones=[]
    for i,a in enumerate(names):
        for b in names[i+1:]:
            groups=defaultdict(list); fa,fb=dims[a],dims[b]
            for r in rows: groups[(fa(r),fb(r))].append(r)
            for (va,vb),items in groups.items():
                if len(items)<MIN_ZONE_SAMPLES: continue
                st=_stat(items)
                if st['adj']>0 and st['strong']>=15:
                    zones.append({'a':a,'va':va,'b':b,'vb':vb,**st})
    zones.sort(key=lambda z:(z['adj'],z['n']),reverse=True)
    return {'minutes':minutes,'n':len(rows),'zones':zones[:max_zones],'tested_note':'2-factor contexts; future validation required','category_mode':'v2 title reclassification'}

def format_positive_zones(r):
    lines=[f"🔎 Positive Zone Finder · {int(r['minutes']/60)}ч",f"База: {r['n']} · min zone n={MIN_ZONE_SAMPLES}",f"Category: {r.get('category_mode','stored')}",""]
    if not r['zones']:
        lines.append('Положительных зон с достаточной выборкой пока не найдено.')
    else:
        lines.append('Исторические кандидаты (не торговый сигнал):')
        for z in r['zones']:
            lines.append(f"• {z['a']}={z['va']} × {z['b']}={z['vb']} · n={z['n']} · adj {z['adj']:+.1f}% · Strong {z['strong']:.1f}%")
    lines += ["","⚠️ Это discovery на истории, не доказанный edge. Зоны должны подтвердиться future-only до использования в Trade v3."]
    return '\n'.join(lines)
