import sys, json, statistics as st
sys.path.insert(0,'.')
import dd_engine as E
SP='/private/tmp/claude-501/-Users-alekseiterin-Documents-optiver-trading-game/c2e11bf9-5c85-44b9-b9ad-a095c4f00d76/scratchpad/'
r50s=[];r90s=[];ons=[];fk={}
for seed in range(12):
    mkt=E.Market(seed=seed,duration=1800.0); E.make_population(mkt); mkt.run()
    T=[(tr['t'],tr['price'],tr['aggressor_side'],tr['buyer'] if tr['aggressor_side']=='bid' else tr['seller']) for tr in mkt.trades]
    kinds={p.name:p.kind for p in mkt.parts}
    for t,kind,txt,ev in mkt.event_log:
        if kind!='announce': continue
        e=next(x for x in mkt.value.events if abs(x.t-t)<=E.DT+1e-9 and x.text==txt)
        if abs(e.ev_delta)<4: continue
        pre=[p for ts,p,_,_ in T if t-20<=ts<t]; 
        if len(pre)<3: continue
        pre=st.median(pre); post=pre+e.ev_delta; sgn=1 if e.ev_delta>0 else -1
        win=[x for x in T if t<=x[0]<=t+60]
        on=next((x for x in win if sgn*(x[1]-pre)>=2),None)
        if not on: continue
        ons.append(on[0]-t)
        def reach(fr):
            lvl=pre+fr*(post-pre); r=next((x for x in win if sgn*(x[1]-lvl)>=0),None); return r and r[0]-on[0]
        a,b=reach(.5),reach(.9)
        if a is not None:r50s.append(a)
        if b is not None:r90s.append(b)
        aggs=[]
        for x in win:
            if (x[2]=='bid')==(sgn>0) and x[3] not in aggs: aggs.append(x[3])
            if len(aggs)>=3:break
        for a_ in aggs: fk[kinds[a_]]=fk.get(kinds[a_],0)+1
print("SIM: announce->onset med",st.median(ons),"p90",sorted(ons)[int(.9*len(ons))])
print("SIM: r50 med",st.median(r50s),"(n",len(r50s),") r90 med",st.median(r90s),"(n",len(r90s),"of",len(ons),")")
print("SIM first-3 aggressor kinds:",fk)
