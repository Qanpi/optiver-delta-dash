#!/usr/bin/env python3
"""Head-to-head in the calibrated v2 sim: EV vs old-MM vs new-maker-MM vs follower."""
import mock_v2_sim as M
import statistics as st
L = M.LIMIT

def flat(c): return None, None, 0, 0, None

def long_hold(c):
    return None, None, 0, 0, ("buy" if c["pos"] < L and c["t"] < 30 else None)

def pure_ev(c):
    # knows a SLOW/noisy fair; crosses to trade toward it (taker toward fair)
    fair, mid, pos = c["fair"], c["mid"], c["pos"]
    tgt = L if fair > mid + 2 else (-L if fair < mid - 2 else 0)
    take = "buy" if tgt > pos else ("sell" if tgt < pos else None)
    return None, None, 0, 0, take

def old_mm(c):
    # our losing bot: loose quotes + FADE via taking (short rallies / buy dips) + snipe
    pos, mid, h = c["pos"], c["mid"], c["hist"]
    take = None
    if len(h) >= 10:
        if mid > h[-10] + 2 and pos > -L: take = "sell"   # fade the rise (what we did)
        elif mid < h[-10] - 2 and pos < L: take = "buy"
    bid = mid - 2 if pos < L else None
    ask = mid + 2 if pos > -L else None
    return bid, ask, 1, 1, take

def new_mm(c):
    # maker-only: tight two-sided, inventory-skew, NO taking, always on
    pos, mid = c["pos"], c["mid"]
    skew = pos * 0.6
    bid = mid - 1.0 - skew if pos < L else None
    ask = mid + 1.0 - skew if pos > -L else None
    return bid, ask, 1, 1, None

def follower(c):
    # FOLLOW (not first): reactive momentum — go WITH the recent move, hold, few trades
    pos, mid, h = c["pos"], c["mid"], c["hist"]
    tgt = pos
    if len(h) >= 12:
        if mid > h[-12] + 2: tgt = L
        elif mid < h[-12] - 2: tgt = -L
    take = "buy" if tgt > pos else ("sell" if tgt < pos else None)
    return None, None, 0, 0, take

STRATS = [("flat", flat), ("long-hold", long_hold), ("pure-EV (slow fair)", pure_ev),
          ("old-MM (fade+take)", old_mm), ("new-MM (maker-only)", new_mm),
          ("follower (reactive mom)", follower)]

def run(drift, label, runs=300):
    print(f"\n== {label} ==")
    print(f"{'strategy':26}{'mean':>8}{'median':>8}{'win%(>100)':>11}{'trades':>8}")
    rows=[]
    for name, fn in STRATS:
        ps=[]; ts=[]
        for r in range(runs):
            p,n=M.simulate(fn, seed=r, drift=drift); ps.append(p); ts.append(n)
        rows.append((name, st.mean(ps), st.median(ps), 100*sum(1 for x in ps if x>100)/runs, st.mean(ts)))
    for name,me,md,w,tr in sorted(rows,key=lambda x:-x[1]):
        print(f"{name:26}{me:>8.0f}{md:>8.0f}{w:>10.0f}%{tr:>8.0f}")

run(None, "MIXED up/down rounds")
run(+1, "UP rounds only")
run(-1, "DOWN rounds only")

# instant/perfect EV: knows true value now, no lag/noise
def pure_ev_instant(c):
    fair, mid, pos = c["fair_now"], c["mid"], c["pos"]
    tgt = L if fair > mid + 1 else (-L if fair < mid - 1 else 0)
    take = "buy" if tgt > pos else ("sell" if tgt < pos else None)
    return None, None, 0, 0, take

print("\n\n######## EV: INSTANT vs SLOW ########")
import statistics as st2
for drift,lab in [(None,"MIXED"),(+1,"UP"),(-1,"DOWN")]:
    rr=300
    ins=[M.simulate(pure_ev_instant,seed=r,drift=drift)[0] for r in range(rr)]
    slo=[M.simulate(pure_ev,seed=r,drift=drift)[0] for r in range(rr)]
    nmm=[M.simulate(new_mm,seed=r,drift=drift)[0] for r in range(rr)]
    print(f"{lab:6}: EV-instant {st2.mean(ins):+7.0f} (win {100*sum(1 for x in ins if x>100)/rr:.0f}%)  |  EV-slow {st2.mean(slo):+7.0f}  |  new-MM {st2.mean(nmm):+7.0f}")
