import json, statistics as st, collections
d=json.load(open('event_data_2026-10-23/reprice/tape.json'))
T=[(v[0],v[1],v[2],v[3]) for _,v in d['trades']]
t0=T[0][0]
def med(a,b):
    x=[p for ts,p,_,_ in T if a<=ts<b]; return st.median(x) if len(x)>=3 else None
cand=[]
for ts,p,_,_ in T:
    pre=med(ts-20,ts-2); post=med(ts+6,ts+25)
    if pre is None or post is None: continue
    cand.append((abs(post-pre),ts,pre,post))
cand.sort(reverse=True); J=[]
for g,ts,pre,post in cand:
    if g<4: break
    if all(abs(ts-j[1])>25 for j in J): J.append((g,ts,pre,post))
J.sort(key=lambda j:j[1])
first=collections.Counter(); lagall=[]
for g,tc,pre,post in J:
    sgn=1 if post>pre else -1
    # onset: first trade from tc-15 that prints >=2 beyond pre in jump dir
    win=[x for x in T if tc-15<=x[0]<=tc+40]
    on=next((x for x in win if sgn*(x[1]-pre)>=2),None)
    if not on: continue
    ton=on[0]
    def reach(fr):
        lvl=pre+fr*(post-pre)
        r=next((x for x in win if x[0]>=ton and sgn*(x[1]-lvl)>=0),None); return r and r[0]-ton
    r50,r90=reach(.5),reach(.9)
    aggs=[]; 
    for x in win:
        if x[0]>=ton-1 and ((x[2]=='bid')==(sgn>0)) and x[3] not in [a for a,_ in aggs]:
            aggs.append((x[3],round(x[0]-ton,1)))
        if len(aggs)>=5: break
    nin=sum(1 for x in win if ton<=x[0]<=ton+10)
    for a,_ in aggs[:3]: first[a]+=1
    lagall.append((r50,r90))
    print(f"t={ton-t0:5.0f}s {pre:4.1f}->{post:4.1f} ({sgn*g:+.1f}) t50={r50} t90={r90} trades/10s={nin} first: {aggs}")
print("\nr50 med",st.median([a for a,b in lagall if a is not None]),"r90 med",st.median([b for a,b in lagall if b is not None]))
print("most-often first-3 aggressors:",first.most_common(10))
