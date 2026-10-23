#!/usr/bin/env python3
"""Strategies vs REAL recorded RAIN book, realistic taker fills. TIME-ORDERED."""
import json, glob
LIMIT=5
def load():
    s=[]
    for f in glob.glob('frames/*.jsonl'):
        for l in open(f):
            try: d=json.loads(l)
            except: continue
            fr=d.get('frame',{}); ts=d.get('ts')
            if isinstance(fr,dict) and fr.get('type')=='shared_update' and ts:
                for ob in fr.get('order_books',[]):
                    if ob.get('instrument_id')=='RAIN':
                        b=ob.get('bids') or []; a=ob.get('asks') or []
                        bb=b[0].get('price') if b and isinstance(b[0],dict) else None
                        ba=a[0].get('price') if a and isinstance(a[0],dict) else None
                        if bb is not None and ba is not None: s.append((ts,bb,ba))
    s.sort(key=lambda x:x[0])            # chronological
    # drop exact-duplicate consecutive snapshots
    out=[]
    for ts,bb,ba in s:
        if not out or (out[-1][0]!=bb or out[-1][1]!=ba): out.append((bb,ba))
    return out
S=load()
def run(strat):
    pos=0;cash=0.0;hist=[];ntr=0
    for i,(bb,ba) in enumerate(S):
        mid=(bb+ba)/2;hist.append(mid)
        tgt=max(-LIMIT,min(LIMIT,strat(i,mid,hist,pos)))
        while pos<tgt:cash-=ba;pos+=1;ntr+=1
        while pos>tgt:cash+=bb;pos-=1;ntr+=1
    settle=(S[-1][0]+S[-1][1])/2
    return cash+pos*settle,ntr
def flat(i,m,h,p):return 0
def long_hold(i,m,h,p):return LIMIT
def short_hold(i,m,h,p):return -LIMIT
def fade(i,m,h,p):
    if len(h)<20:return 0
    a=sum(h[-20:])/20;return -LIMIT if m>a+1 else(LIMIT if m<a-1 else p)
def throttled_fade(i,m,h,p):
    if i%25!=0 or len(h)<20:return p
    a=sum(h[-20:])/20;return -LIMIT if m>a+1 else(LIMIT if m<a-1 else p)
def mom_hold(i,m,h,p):
    if len(h)<30:return 0
    if m>h[-30]+2 and p<LIMIT:return LIMIT
    if m<h[-30]-2 and p>-LIMIT:return -LIMIT
    return p
def early_long(i,m,h,p):return LIMIT if i>30 else 0
def breakout_hold(i,m,h,p):
    if len(h)<10:return p
    if p==0:
        if m>h[-10]+5:return LIMIT
        if m<h[-10]-5:return -LIMIT
    return p
print(f"real RAIN path (time-ordered): {len(S)} ticks, open {(S[0][0]+S[0][1])/2:.0f} close {(S[-1][0]+S[-1][1])/2:.0f}, min {min((b+a)/2 for b,a in S):.0f} max {max((b+a)/2 for b,a in S):.0f}\n")
res=[]
for n,fn in [('flat/no-trade',flat),('long-hold',long_hold),('short-hold',short_hold),('fade(churn)',fade),('throttled-fade',throttled_fade),('momentum-hold',mom_hold),('early-long-hold',early_long),('breakout-hold',breakout_hold)]:
    pnl,ntr=run(fn);res.append((n,pnl,ntr))
print("ranked by PnL (realistic taker, pays spread each trade):")
for n,pnl,ntr in sorted(res,key=lambda x:-x[1]):
    print(f"  {n:18}{pnl:+8.0f}  ({ntr} trades)")
