#!/usr/bin/env python3
"""Replay backtester: trade a strategy against the REAL recorded RAIN order book.
Faithful to what actually happened — no agent assumptions. Fills at recorded
best bid/ask, enforces +/-5 limit, settles at the final mid."""
import json, glob
LIMIT = 5

def load_book_series():
    s = []
    for f in sorted(glob.glob('frames/*.jsonl')):
        for l in open(f):
            try: d = json.loads(l)
            except: continue
            fr = d.get('frame', {})
            if isinstance(fr, dict) and fr.get('type') == 'shared_update':
                for ob in fr.get('order_books', []):
                    if ob.get('instrument_id') == 'RAIN':
                        b = ob.get('bids') or []; a = ob.get('asks') or []
                        bb = b[0].get('price') if b and isinstance(b[0], dict) else None
                        ba = a[0].get('price') if a and isinstance(a[0], dict) else None
                        if bb is not None and ba is not None:
                            s.append((d.get('ts'), bb, ba))
    s.sort()
    return s

def backtest(strategy, series):
    """strategy(i, mid, hist, pos) -> desired target position in [-5,5]."""
    pos = 0; cash = 0.0; hist = []
    for i, (ts, bb, ba) in enumerate(series):
        mid = (bb + ba) / 2
        hist.append(mid)
        tgt = max(-LIMIT, min(LIMIT, strategy(i, mid, hist, pos)))
        # move toward target by crossing the spread (buy at ask, sell at bid)
        while pos < tgt:               # buy
            cash -= ba; pos += 1
        while pos > tgt:               # sell
            cash += bb; pos -= 1
    settle = (series[-1][1] + series[-1][2]) / 2
    return cash + pos * settle        # mark final inventory at settlement

series = load_book_series()
print(f"replayed {len(series)} real RAIN ticks, open {(series[0][1]+series[0][2])/2:.0f} close {(series[-1][1]+series[-1][2])/2:.0f}\n")

def s_long_hold(i, mid, hist, pos):   return LIMIT                       # buy 5, hold
def s_short_hold(i, mid, hist, pos):  return -LIMIT                      # what we did
def s_fade(i, mid, hist, pos):        # mean-reversion: short above recent avg, long below
    if len(hist) < 20: return 0
    avg = sum(hist[-20:]) / 20
    return -LIMIT if mid > avg + 1 else (LIMIT if mid < avg - 1 else pos)
def s_trend(i, mid, hist, pos):       # momentum: long if rising, short if falling
    if len(hist) < 20: return 0
    return LIMIT if mid > hist[-20] else (-LIMIT if mid < hist[-20] else pos)

for name, fn in [("long-hold", s_long_hold), ("short-hold", s_short_hold),
                 ("fade-MM (ours)", s_fade), ("trend-follow", s_trend)]:
    print(f"  {name:16}: {backtest(fn, series):+8.0f} pts")
print("\n(winner on the day had ~124)")
