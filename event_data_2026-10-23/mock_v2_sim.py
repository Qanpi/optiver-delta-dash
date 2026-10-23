#!/usr/bin/env python3
"""
mock_v2_sim — offline Delta Dash simulator calibrated to the real RAIN capture.

Purpose: develop strategies against a market that behaves like the REAL one, where
the key mechanic is ADVERSE SELECTION — a resting market-maker gets filled on the
wrong side right when the value jumps, but earns the spread during calm noise flow.

Calibration targets (from event_data_2026-10-23):
  - value is a STEP function: base (word letter-sum) + ~15 event jumps; TRENDS
    (direction randomizable per run); bounded 0..100.
  - ~80 direction changes / lots of swings; perfect swing-MM ceiling ~1700 pts.
  - spread median ~2 (tick 1), blows out after jumps.
  - informed flow picks off stale quotes at jumps; noise flow pays the spread.

Validation goal: a GOOD mm (calm-tight / jump-wide) >> naive-mm ~ 0; long-hold ~ safe;
and this must hold across UP and DOWN rounds.
"""
from __future__ import annotations
import random, statistics as st

TICK = 1.0
LO, HI = 0.0, 100.0
LIMIT = 5
N_EVENTS = 15


def clamp(x):
    return max(LO, min(HI, x))


def make_value_path(steps=1000, seed=None, drift=None):
    """True value V over time: starts at a base, jumps at N_EVENTS scheduled times.
    drift in {+1,-1,None}: bias of the net event direction (None = random)."""
    rng = random.Random(seed)
    base = rng.choice([24, 30, 40, 55, 66])          # word-letter-sum-like base
    V = float(base)
    if drift is None:
        drift = rng.choice([+1, -1])
    path = []
    event_steps = sorted(rng.sample(range(20, steps - 20), N_EVENTS))
    ei = 0
    for t in range(steps):
        if ei < len(event_steps) and t == event_steps[ei]:
            ei += 1
            kind = rng.choice(["mult", "add", "pct", "shock"])
            if kind == "mult":
                V *= rng.choice([2.0, 0.5])            # 50/50 double/halve realized
            elif kind == "add":
                V += drift * rng.uniform(5, 25)
            elif kind == "pct":
                V *= 1 + drift * rng.uniform(0.1, 0.35)
            else:                                     # shock (can go against drift)
                V += rng.choice([-1, 1]) * rng.uniform(10, 30)
            V = clamp(V)
        # small between-event noise
        V = clamp(V + rng.gauss(0, 0.3))
        path.append((V, t in set(event_steps[:ei]) and ei > 0 and event_steps[ei-1] == t))
    return [v for v, _ in path], set(event_steps)


def simulate(strategy, steps=1000, seed=None, drift=None, verbose=False):
    """Run a market with informed + noise flow against a strategy. Returns PnL, n_trades.
    strategy(ctx) -> (bid_px, ask_px, bid_sz, ask_sz, take) where `take` optionally
    crosses: 'buy'/'sell'/None. The MM posts bid/ask; fills come from flow."""
    rng = random.Random((seed or 0) + 777)
    V, events = make_value_path(steps, seed, drift)
    pos = 0
    cash = 0.0
    ntr = 0
    # "market mid" lags the true value with overshoot (the crowd)
    M = V[0]
    hist = []
    for t in range(steps):
        v = V[t]
        # crowd pulls market mid toward value, with lag + a little overshoot
        jump = t in events
        alpha = 0.6 if jump else 0.08
        M += alpha * (v - M) + rng.gauss(0, 0.4)
        M = clamp(M)
        hist.append(M)
        spread = 2.0 + (8.0 if jump else 0.0) + abs(rng.gauss(0, 0.5))
        mbid, mask = M - spread / 2, M + spread / 2

        fair = clamp(V[max(0, t-10)] + rng.gauss(0, 1.0))  # hint-derived fair, SLOW (lag 10) + noisy
        fair_now = V[t]  # PERFECT instant fair (idealized EV)
        ctx = dict(t=t, mid=M, hist=hist, pos=pos, jump=jump, mbid=mbid, mask=mask, limit=LIMIT, fair=fair, fair_now=fair_now)
        bid_px, ask_px, bsz, asz, take = strategy(ctx)

        # 1) optional taker action (cross the market spread)
        if take == "buy" and pos < LIMIT:
            cash -= mask; pos += 1; ntr += 1
        elif take == "sell" and pos > -LIMIT:
            cash += mbid; pos -= 1; ntr += 1

        # 2) our resting quotes get filled by flow
        # informed flow at/just-before a jump: trades THROUGH our stale quote on the
        # side that will profit them (= adverse selection against us).
        nxt = V[min(t + 1, steps - 1)]
        if jump or abs(nxt - M) > 3:
            if nxt > M and ask_px is not None and ask_px <= nxt and pos > -LIMIT:
                # value rising: informed lift our ask -> we sold low (adverse)
                cash += ask_px; pos -= 1; ntr += 1
            elif nxt < M and bid_px is not None and bid_px >= nxt and pos < LIMIT:
                cash -= bid_px; pos += 1; ntr += 1
        else:
            # calm: noise flow randomly hits whichever of our quotes is at/through market
            if rng.random() < 0.5:
                if bid_px is not None and bid_px >= mbid and pos < LIMIT:
                    cash -= bid_px; pos += 1; ntr += 1   # someone sells to us at our bid
            else:
                if ask_px is not None and ask_px <= mask and pos > -LIMIT:
                    cash += ask_px; pos -= 1; ntr += 1   # someone buys our ask

    settle = V[-1]
    pnl = cash + pos * settle
    if verbose:
        print(f"  V {V[0]:.0f}->{settle:.0f} (drift path), mid range {min(hist):.0f}-{max(hist):.0f}, trades {ntr}, pos {pos}")
    return pnl, ntr


# ---------------- strategies ----------------
def s_flat(ctx):
    return None, None, 0, 0, None

def s_long_hold(ctx):
    # buy to +limit early via taker, then rest (no quotes)
    take = "buy" if ctx["pos"] < LIMIT and ctx["t"] < 30 else None
    return None, None, 0, 0, take

def s_naive_mm(ctx):
    # always quote tight around mid — gets adversely selected at every jump
    if abs(ctx["pos"]) >= LIMIT:
        # skew to shed inventory
        if ctx["pos"] > 0: return None, ctx["mid"], 0, 1, None
        else: return ctx["mid"], None, 1, 0, None
    return ctx["mid"] - 1, ctx["mid"] + 1, 1, 1, None

def s_good_mm(ctx):
    # tight when calm, PULL quotes at/near a jump (avoid adverse selection), inventory-skew
    pos = ctx["pos"]; mid = ctx["mid"]
    if ctx["jump"]:
        return None, None, 0, 0, None          # stand aside through the jump
    skew = pos * 0.6                            # lean quotes to mean-revert inventory to 0
    bid = mid - 1.5 - skew if pos < LIMIT else None
    ask = mid + 1.5 - skew if pos > -LIMIT else None
    return bid, ask, 1, 1, None

STRATS = [("flat", s_flat), ("long-hold", s_long_hold),
          ("naive-mm", s_naive_mm), ("good-mm (calm-tight/jump-wide)", s_good_mm)]


def evaluate(runs=200):
    print(f"mock_v2 validation — {runs} runs each, mixed up/down rounds\n")
    print(f"{'strategy':34}{'mean PnL':>10}{'win#1-ish':>10}{'median':>9}{'trades':>8}")
    print("-" * 71)
    for name, fn in STRATS:
        pnls = []; trs = []
        for r in range(runs):
            pnl, ntr = simulate(fn, seed=r, drift=None)
            pnls.append(pnl); trs.append(ntr)
        pos_rate = 100 * sum(1 for p in pnls if p > 100) / runs
        print(f"{name:34}{st.mean(pnls):>10.0f}{pos_rate:>9.0f}%{st.median(pnls):>9.0f}{st.mean(trs):>8.0f}")
    # split by up vs down rounds to check robustness
    print("\n-- by round direction (good-mm vs long-hold) --")
    for lab, dr in [("UP rounds", +1), ("DOWN rounds", -1)]:
        g = st.mean(simulate(s_good_mm, seed=r, drift=dr)[0] for r in range(runs))
        l = st.mean(simulate(s_long_hold, seed=r, drift=dr)[0] for r in range(runs))
        print(f"  {lab:12}: good-mm {g:+7.0f}   long-hold {l:+7.0f}")


if __name__ == "__main__":
    evaluate()
