#!/usr/bin/env python3
"""Replay backtest with MID-price fills (no spread/fees) — isolates the pure
DIRECTIONAL effect from spread-crossing cost."""
import json, glob
LIMIT = 5
def load():
    s=[]
    for f in sorted(glob.glob('frames/*.jsonl')):
        for l in open(f):
            try: d=json.loads(l)
            except: continue
            fr=d.get('frame',{})
            if isinstance(fr,dict) and fr.get('type')=='shared_update':
                for ob in fr.get('order_books',[]):
                    if ob.get('instrument_id')=='RAIN':
                        b=ob.get('bids') or []; a=ob.get('asks') or []
                        bb=b[0].get('price') if b and isinstance(b[0],dict) else None
                        ba=a[0].get('price') if a and isinstance(a[0],dict) else None
                        if bb is not None and ba is not None: s.append((d.get('ts'),(bb+ba)/2))
    s.sort(); return s
def bt(strat, series, cross=False, half_spread=0.5):
    pos=0; cash=0.0; hist=[]
    for i,(ts,mid) in enumerate(series):
        hist.append(mid)
        tgt=max(-LIMIT,min(LIMIT,strat(i,mid,hist,pos)))
        while pos<tgt: cash-=mid+(half_spread if cross else 0); pos+=1
        while pos>tgt: cash+=mid-(half_spread if cross else 0); pos-=1
    return cash+pos*series[-1][1]
s=load()
def long_hold(i,m,h,p): return LIMIT
def short_hold(i,m,h,p): return -LIMIT
def fade(i,m,h,p):
    if len(h)<20: return 0
    a=sum(h[-20:])/20; return -LIMIT if m>a+1 else (LIMIT if m<a-1 else p)
def trend(i,m,h,p):
    if len(h)<20: return 0
    return LIMIT if m>h[-20] else (-LIMIT if m<h[-20] else p)
print(f"{len(s)} ticks, open {s[0][1]:.0f} close {s[-1][1]:.0f}\n")
print(f"{'strategy':16} {'MID-fill (no cost)':>20} {'cross-spread':>14}")
for n,fn in [('long-hold',long_hold),('short-hold',short_hold),('fade-MM',fade),('trend-follow',trend)]:
    print(f"{n:16} {bt(fn,s):>20.0f} {bt(fn,s,cross=True):>14.0f}")
