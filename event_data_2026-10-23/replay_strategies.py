#!/usr/bin/env python3
"""
Replay each strategy against the REAL captured RAIN market and ask: would we have won?

Faithful counterfactual (holding everyone else's behavior fixed):
  - MAKER fills: our resting quote is filled when a REAL trade prints at/through our price
    (capped at the real trade's volume — we can't fill more than actually traded).
  - TAKER actions: cross the real top-of-book.
  - settle remaining inventory at the real final mid.
Caveat: ignores our own market impact (our quotes would move others). Standard replay
assumption; "all other real data the same, to the extent possible."
"""
import json, glob, bisect
LIMIT = 5
WIN_TARGET = 902  # the real winner's score

def load():
    ev = []  # (ts, kind, ...)
    seen = set()
    for f in glob.glob('frames/*.jsonl'):
        for l in open(f):
            try: d = json.loads(l)
            except: continue
            fr = d.get('frame', {}); ts = d.get('ts')
            if not (isinstance(fr, dict) and fr.get('type') == 'shared_update' and ts): continue
            for ob in fr.get('order_books', []):
                if ob.get('instrument_id') == 'RAIN':
                    b = ob.get('bids') or []; a = ob.get('asks') or []
                    bb = b[0].get('price') if b and isinstance(b[0], dict) else None
                    ba = a[0].get('price') if a and isinstance(a[0], dict) else None
                    if bb is not None and ba is not None:
                        ev.append((ts, 'book', bb, ba))
            for tr in fr.get('trades', []):
                if tr.get('instrument_id') == 'RAIN' and tr.get('trade_id') not in seen:
                    seen.add(tr.get('trade_id'))
                    ev.append((ts, 'trade', float(tr['price']), tr.get('aggressor_side'),
                               tr.get('volume', 1), tr.get('buyer'), tr.get('seller')))
    ev.sort(key=lambda x: x[0])
    return ev

EV = load()

def replay(strategy, maker=True):
    """strategy(ctx)->(bid,ask,take). ctx has mid,bb,ba,hist,pos,fair_now(approx),t."""
    pos = 0; cash = 0.0; ntr = 0
    bb = ba = None; hist = []; my_bid = my_ask = None
    # crude 'fair proxy' from a short-forward look is NOT allowed (no lookahead);
    # we give strategies only past/current info.
    for i, e in enumerate(EV):
        if e[1] == 'book':
            bb, ba = e[2], e[3]; mid = (bb + ba) / 2; hist.append(mid)
            ctx = dict(t=i, mid=mid, bb=bb, ba=ba, hist=hist, pos=pos, limit=LIMIT)
            my_bid, my_ask, take = strategy(ctx)
            if take == 'buy' and pos < LIMIT and ba is not None:
                cash -= ba; pos += 1; ntr += 1
            elif take == 'sell' and pos > -LIMIT and bb is not None:
                cash += bb; pos -= 1; ntr += 1
        else:  # a real trade prints -> does it fill our resting quote?
            if not maker: continue
            ptr, aggr, vol = e[2], e[3], e[4]
            vol = int(vol) if vol else 1
            if aggr == 'ask' and my_bid is not None and my_bid >= ptr and pos < LIMIT:
                # a seller aggressed through our (better) bid -> we buy at our bid
                q = min(vol, LIMIT - pos)
                cash -= my_bid * q; pos += q; ntr += q
            elif aggr == 'bid' and my_ask is not None and my_ask <= ptr and pos > -LIMIT:
                q = min(vol, LIMIT + pos)
                cash += my_ask * q; pos -= q; ntr += q
    settle = (EV[-1][2] + EV[-1][3]) / 2 if EV[-1][1] == 'book' else hist[-1]
    # find last book for settle
    for e in reversed(EV):
        if e[1] == 'book': settle = (e[2] + e[3]) / 2; break
    return cash + pos * settle, ntr

# ---- strategies (same shapes as the sim, adapted; no lookahead) ----
def flat(c): return None, None, None
def long_hold(c): return None, None, ('buy' if c['pos'] < LIMIT and c['t'] < 20 else None)
def old_mm(c):   # fade + take
    pos, mid, h = c['pos'], c['mid'], c['hist']; take = None
    if len(h) >= 10:
        if mid > h[-10] + 2 and pos > -LIMIT: take = 'sell'
        elif mid < h[-10] - 2 and pos < LIMIT: take = 'buy'
    return (c['bb'] if pos < LIMIT else None), (c['ba'] if pos > -LIMIT else None), take
def new_mm(c):   # maker-only, tight, skew, no take
    pos, bb, ba = c['pos'], c['bb'], c['ba']; mid = (bb + ba) / 2; skew = pos * 0.6
    return (round(mid - 1 - skew) if pos < LIMIT else None,
            round(mid + 1 - skew) if pos > -LIMIT else None, None)
def follower(c):
    pos, mid, h = c['pos'], c['mid'], c['hist']; tgt = pos
    if len(h) >= 12:
        if mid > h[-12] + 2: tgt = LIMIT
        elif mid < h[-12] - 2: tgt = -LIMIT
    return None, None, ('buy' if tgt > pos else ('sell' if tgt < pos else None))

print(f"replaying {sum(1 for e in EV if e[1]=='book')} book updates + {sum(1 for e in EV if e[1]=='trade')} real trades")
print(f"winner to beat: {WIN_TARGET}\n")
print(f"{'strategy':22}{'PnL':>8}{'trades':>8}  would-win?")
for name, fn, mk in [('flat', flat, False), ('long-hold', long_hold, False),
                     ('old-MM (fade+take)', old_mm, True), ('new-MM (maker-only)', new_mm, True),
                     ('follower', follower, False)]:
    pnl, ntr = replay(fn, maker=mk)
    print(f"{name:22}{pnl:>8.0f}{ntr:>8}  {'YES 🏆' if pnl >= WIN_TARGET else ('top-ish' if pnl>300 else 'no')}")
