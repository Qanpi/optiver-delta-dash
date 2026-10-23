"""Strategy E feasibility on the REAL 2026-10-23 RAIN book: how often was best_ask - best_bid >= 3,
and how much could a main/feeder ring have transferred at a given legs/min budget?
Each leg = main rests 1 lot one tick inside, a feeder crosses it. Main gains vs mid:
(mid - (bb+1)) on a buy leg or ((ba-1) - mid) on a sell leg = gap/2 - 1 per leg."""
import json, sys, statistics as st, collections
frames = []
for fn in sys.argv[1:]:
    for l in open(fn):
        d = json.loads(l); f = d.get('frame')
        if not isinstance(f, dict) or f.get('type') not in ('shared_update', None): continue
        for ob in f.get('order_books') or []:
            if ob['instrument_id'] != 'RAIN': continue
            bids = [b['price'] for b in ob['bids'] if b.get('bid', 0) > 0]
            asks = [a['price'] for a in ob['asks'] if a.get('ask', 0) > 0]
            if bids and asks: frames.append((d['ts'], max(bids), min(asks)))
        lb = f.get('leaderboard')
        if lb: last_lb = lb
frames.sort()
frames = [x for i, x in enumerate(frames) if i == 0 or x[0] - frames[i-1][0] > 0.05]
gaps = [ba - bb for _, bb, ba in frames]
span = (frames[-1][0] - frames[0][0]) / 60
print(f"book frames {len(frames)} over {span:.1f} min")
c = collections.Counter(min(int(g), 10) for g in gaps)
print("spread distribution:", {k: f"{100*v/len(gaps):.0f}%" for k, v in sorted(c.items())})
ok = [g for g in gaps if g >= 3]
print(f"frames with spread >= 3: {100*len(ok)/len(gaps):.0f}%, mean gain/leg there {st.mean(g/2-1 for g in ok):.2f}")
# time-weighted: each frame holds until the next
dur = collections.defaultdict(float)
for (t, bb, ba), (t2, _, _) in zip(frames, frames[1:]):
    dur[ba - bb >= 3] += min(t2 - t, 2.0)
frac = dur[True] / (dur[True] + dur[False])
gain = sum(min(t2 - t, 2.0) * ((ba - bb) / 2 - 1) for (t, bb, ba), (t2, _, _) in zip(frames, frames[1:]) if ba - bb >= 3) / dur[True]
print(f"time with spread >= 3: {100*frac:.0f}%, time-weighted gain/leg {gain:.2f}")
lbv = sorted((r[1] for r in last_lb), reverse=True)
print("real final board top5:", [round(x) for x in lbv[:5]])
print("\nprojected MAIN PnL for a 30-min round (ring legs only; main otherwise flat):")
for lpm in (30, 60, 120, 240):
    pnl = lpm * 30 * frac * gain
    rank = 1 + sum(v > pnl for v in lbv)
    print(f"  {lpm:3} legs/min while open -> +{pnl:5.0f}  (rank {rank} on the real final board)")
