import json, sys, statistics as st
mids=[]; trades={}
for l in open(sys.argv[1]):
    d=json.loads(l); f=d.get('frame'); ts=d['ts']
    if not isinstance(f,dict): continue
    for ob in f.get('order_books') or []:
        if ob['instrument_id']=='RAIN' and ob['bids'] and ob['asks']:
            bb=max(x['price'] for x in ob['bids']); ba=min(x['price'] for x in ob['asks'])
            if ba-bb<=8: mids.append((ts,(bb+ba)/2,bb,ba))
    for t in f.get('trades') or []:
        if t['instrument_id']=='RAIN' and t['trade_id'] not in trades:
            ag=t['buyer'] if t['aggressor_side']=='bid' else t['seller']
            trades[t['trade_id']]=(ts,float(t['price']),t['aggressor_side'],ag,t['buyer'],t['seller'])
json.dump({'mids':mids,'trades':sorted(trades.items())},open(sys.argv[2],'w'))
print(len(mids),len(trades),mids[0][0],mids[-1][0])
