#!/usr/bin/env python3
"""
agent_sim — run the zero-sum agent market (dd_engine) and validate it against the real
RAIN leaderboard, then bench strategies-under-test inside it by RANK, not raw PnL.

Two modes:
  python3 agent_sim.py calibrate [runs]   # no strategy under test: does the agent-only
                                           # leaderboard reproduce the real distribution?
  python3 agent_sim.py bench [runs]        # drop each candidate strategy in, report mean
                                           # rank / win-rate across up AND down rounds.
  python3 agent_sim.py slide [runs] [naive_coin]  # slide-reader delay sweep vs copy-top1

Real targets (final pre-settlement, 77 players): mean 0 (zero-sum), stdev ~305,
top5 ~[952,791,692,506,433], worst ~-1040, ~10% flat at 0, 44% profitable.
Context from fair-path analysis: quiet baseline + ~15 jumps, jumps balanced up/down,
frequent reversals, wide range; NET DIRECTION RANDOM per round (no up-bias assumption).
"""
from __future__ import annotations
import sys, statistics as st
import dd_engine as E
from dd_engine import Participant, LIMIT, clamp


# ---------------------------------------------------------------- strategies under test
class StratFlat(Participant):
    """Do nothing. Benchmark: a fresh 0 already beats the losing tail."""
    kind = "SUT_flat"


class StratMakerOnly(Participant):
    """Pure maker: quote tight around mid when calm, pull on events, inventory-skew.
    Never crosses the spread (no taker churn). Our 'new MM'."""
    kind = "SUT_maker"

    def __init__(self, name, rng):
        super().__init__(name, rng)
        self.cool = 0.0

    def on_event(self, mkt, kind, e):
        self.cool = mkt.t + 6.0          # stand aside 6s around a jump (avoid adverse fill)
        mkt.cancel_all(self)

    def on_step(self, mkt):
        if mkt.t < self.cool:
            return
        if self.rng.random() > 0.3:
            return
        mkt.cancel_all(self)
        mid = mkt.mid()
        skew = self.pos * 0.8
        if self.pos < LIMIT:
            mkt.insert(self, "bid", round(mid - 1 - skew), 1)
        if self.pos > -LIMIT:
            mkt.insert(self, "ask", round(mid + 1 - skew), 1)


class StratFollowMaker(Participant):
    """Maker that leans its quotes toward recent momentum: skews the quote the way price
    just moved, so it accumulates WITH a trend instead of fading it. Still maker-only."""
    kind = "SUT_follow"

    def __init__(self, name, rng):
        super().__init__(name, rng)
        self.cool = 0.0

    def on_event(self, mkt, kind, e):
        self.cool = mkt.t + 4.0
        mkt.cancel_all(self)

    def on_step(self, mkt):
        if mkt.t < self.cool or self.rng.random() > 0.3:
            return
        mkt.cancel_all(self)
        mid = mkt.mid()
        px = mkt.recent_prices(15)
        mom = (px[-1] - px[0]) if len(px) >= 2 else 0
        lean = max(-2, min(2, mom * 0.3))       # shift both quotes toward the move
        skew = self.pos * 0.8
        if self.pos < LIMIT:
            mkt.insert(self, "bid", round(mid - 1 + lean - skew), 1)
        if self.pos > -LIMIT:
            mkt.insert(self, "ask", round(mid + 1 + lean - skew), 1)


class StratInstantEV(Participant):
    """Idealized: knows public EV instantly, takes toward it past an edge. Upper bound on
    'fair-chasing done perfectly'. (We could NOT do this live; context benchmark.)"""
    kind = "SUT_ev"

    def on_step(self, mkt):
        ev = mkt.value.ev
        bb, ba = mkt.book.best_bid(), mkt.book.best_ask()
        if ba is not None and ba <= ev - 2 and self.pos < LIMIT:
            mkt.insert(self, "bid", ba, 1)
        elif bb is not None and bb >= ev + 2 and self.pos > -LIMIT:
            mkt.insert(self, "ask", bb, 1)


class StratOldMM(Participant):
    """Our live config: fade + take. Mean-reversion against the move. Expect: loses."""
    kind = "SUT_oldmm"

    def on_step(self, mkt):
        mid = mkt.mid()
        px = mkt.recent_prices(20)
        if len(px) >= 2:
            mv = px[-1] - px[0]
            if mv > 2 and self.pos > -LIMIT:
                b = mkt.book.best_bid()
                if b is not None: mkt.insert(self, "ask", b, 1)      # sell the rally
            elif mv < -2 and self.pos < LIMIT:
                a = mkt.book.best_ask()
                if a is not None: mkt.insert(self, "bid", a, 1)      # buy the dip
        if self.rng.random() < 0.3:
            mkt.cancel_all(self)
            if self.pos < LIMIT: mkt.insert(self, "bid", round(mid - 1), 1)
            if self.pos > -LIMIT: mkt.insert(self, "ask", round(mid + 1), 1)


class StratSlideReader(Participant):
    """Human-speed hint reader (open question 1): sees each announce/resolve on the slide,
    gets the new EV `delay` seconds later (read + key the event type into a parser that does
    the math, coin priced x1.25), then sweeps the book to its target and holds. Between
    events it keeps taking anything that crosses target +/- edge. Never quotes passively."""
    kind = "SUT_slide"
    delay = (3.0, 6.0); err = 0.0; edge = 2; minmove = 0
    persist = float("inf")      # seconds after a fresh read during which it may take

    def __init__(self, name, rng):
        super().__init__(name, rng)
        self.target = None; self.pending = []; self.read_t = 0.0

    def on_event(self, mkt, kind, e):
        if abs(getattr(e, "ev_delta", 0)) < self.minmove and kind != "resolve":
            return
        tgt = mkt.value.ev + (self.rng.gauss(0, self.err) if self.err else 0)
        self.pending.append((mkt.t + self.rng.uniform(*self.delay), clamp(tgt)))

    def on_step(self, mkt):
        for item in list(self.pending):
            if item[0] <= mkt.t:
                self.pending.remove(item); self.target = item[1]; self.read_t = mkt.t
        if self.target is None or mkt.t - self.read_t > self.persist:
            return
        bb, ba = mkt.book.best_bid(), mkt.book.best_ask()
        if ba is not None and ba <= self.target - self.edge and self.pos < LIMIT:
            mkt.insert(self, "bid", int(self.target - self.edge), LIMIT - self.pos)
        elif bb is not None and bb >= self.target + self.edge and self.pos > -LIMIT:
            mkt.insert(self, "ask", int(round(self.target + self.edge)), LIMIT + self.pos)


def slide_cls(delay, err=0.0, edge=2, persist=float("inf")):
    return type(f"Slide_{delay}_{err}", (StratSlideReader,),
                dict(delay=delay, err=err, edge=edge, persist=persist))


class StratCopyTop1(Participant):
    """Hint-free (mm_bot --strategy copy --copy-k 1): every 5s pick the current PnL leader,
    follow the direction of its aggressive prints by crossing to +/-LIMIT, hold until it
    aggresses the other way. Fill budget per minute caps churn."""
    kind = "SUT_copy"
    budget = 6

    def __init__(self, name, rng):
        super().__init__(name, rng)
        self.seen = 0; self.leader = None; self.next_lb = 0.0; self.want = 0
        self.fills = []

    def on_fill(self, mkt, side, price, maker):
        self.fills.append(mkt.t)

    def on_step(self, mkt):
        if mkt.t >= self.next_lb:
            m = mkt.mark()
            others = [p for p in mkt.parts if p is not self]
            self.leader = max(others, key=lambda p: p.pnl(m)).name
            self.next_lb = mkt.t + 5.0
        for tr in mkt.trades[self.seen:]:
            ag = tr["buyer"] if tr["aggressor_side"] == "bid" else tr["seller"]
            if ag == self.leader:
                self.want = LIMIT if tr["aggressor_side"] == "bid" else -LIMIT
        self.seen = len(mkt.trades)
        self.fills = [t for t in self.fills if t > mkt.t - 60]
        if len(self.fills) >= self.budget or self.want == self.pos:
            return
        bb, ba = mkt.book.best_bid(), mkt.book.best_ask()
        n = min(abs(self.want - self.pos), self.budget - len(self.fills))
        if self.want > self.pos and ba is not None:
            mkt.insert(self, "bid", ba, n)
        elif self.want < self.pos and bb is not None:
            mkt.insert(self, "ask", bb, n)


def slide_bench(runs=40, naive_coin=0.0):
    """Open question 1: human-speed slide reader vs copy-top1 vs instant-EV, by delay."""
    print(f"SLIDE-READER BENCH — {runs} runs each, naive_coin={naive_coin}, rank out of 77\n")
    print(f"{'strategy':22}{'mean PnL':>9}{'mean rank':>10}{'med rank':>9}{'top5%':>7}{'win%':>6}{'trades':>7}")
    print("-" * 70)
    rows = [("instant-EV", StratInstantEV), ("copy-top1 b6", StratCopyTop1), ("flat", StratFlat)]
    for d in [(1, 2), (2, 4), (3, 6), (5, 10), (8, 15), (12, 25)]:
        rows.append((f"slide {d[0]}-{d[1]}s err0", slide_cls(d)))
    for d in [(3, 6), (5, 10)]:
        rows.append((f"slide {d[0]}-{d[1]}s err3", slide_cls(d, err=3.0)))
    rows.append(("slide 3-6s edge4", slide_cls((3, 6), edge=4)))
    for d in [(1, 2), (2, 4), (3, 6), (5, 10)]:
        rows.append((f"sweep-once {d[0]}-{d[1]}s", slide_cls(d, persist=2.0)))
    for name, cls in rows:
        pnls = []; ranks = []; ntr = []
        for r in range(runs):
            drift = 1 if r % 2 == 0 else -1
            mkt, lb, sut = run_market(r, drift, cls, naive_coin=naive_coin)
            ranks.append(next(i + 1 for i, (_, _, p) in enumerate(lb) if p is sut))
            pnls.append(sut.pnl(mkt.mark())); ntr.append(sut.n_trades)
        print(f"{name:22}{st.mean(pnls):>9.0f}{st.mean(ranks):>10.1f}{st.median(ranks):>9.0f}"
              f"{100*sum(x<=5 for x in ranks)/runs:>6.0f}%{100*sum(x>5 for x in pnls)/runs:>5.0f}%"
              f"{st.mean(ntr):>7.0f}")


SUTS = [("flat", StratFlat), ("maker-only", StratMakerOnly),
        ("follow-maker", StratFollowMaker), ("instant-EV", StratInstantEV),
        ("old-MM (fade)", StratOldMM)]


# ---------------------------------------------------------------- runs
def run_market(seed, drift, sut_cls=None, feeders=0, naive_coin=0.0):
    mkt = E.Market(seed=seed, duration=1800.0, drift=drift)
    E.make_population(mkt, feeders=feeders, naive_coin=naive_coin)
    sut = None
    if sut_cls is not None:
        sut = sut_cls("SUT", mkt.rng)
        mkt.add(sut)
    mkt.run()
    lb = mkt.leaderboard()
    return mkt, lb, sut


def pct(vals, q):
    s = sorted(vals); return s[min(len(s) - 1, int(len(s) * q))]


def calibrate(runs=60, feeders=0):
    tag = f"with {feeders} feeders" if feeders else "NATURAL field (no feeders)"
    print(f"CALIBRATION — agents only, {runs} runs, {tag}\n")
    if feeders:
        print("event target (our 5 bots fed it): stdev~305, top~950, worst~-1040\n")
    else:
        print("natural target (un-fed, per user): top ≈ +400, worst ≈ -400, tighter spread\n")
    tops = []; worsts = []; sds = []; flats = []; ups = []; bykind = {}
    for r in range(runs):
        mkt, lb, _ = run_market(r, None, feeders=feeders)
        vals = [v for _, v, _ in lb]
        sds.append(st.pstdev(vals))
        tops.append(sorted(vals, reverse=True)[:5])
        worsts.append(min(vals))
        flats.append(100 * sum(1 for v in vals if abs(v) < 5) / len(vals))
        ups.append(100 * sum(1 for v in vals if v > 5) / len(vals))
        for _, v, p in lb:
            bykind.setdefault(p.kind, []).append(v)
    top5 = [round(st.mean(t[i] for t in tops)) for i in range(5)]
    print(f"stdev        mean {st.mean(sds):4.0f}   (target ~305)")
    print(f"top5         {top5}   (target [952,791,692,506,433])")
    print(f"worst        mean {st.mean(worsts):5.0f}   (target ~-1040)")
    print(f"flat %       {st.mean(flats):4.0f}%   (target ~10%)")
    print(f"profitable % {st.mean(ups):4.0f}%   (target ~44%)")
    print(f"zero-sum chk mean-of-means {st.mean(st.mean([v for _,v,_ in run_market(r,None)[1]]) for r in range(5)):+.1f} (must be ~0)")
    print("\nPnL by agent type (mean / best) — informed+makers should win, churners lose:")
    for k, vs in sorted(bykind.items(), key=lambda kv: -st.mean(kv[1])):
        print(f"  {k:9} mean {st.mean(vs):+7.1f}  best {max(vs):+7.0f}  worst {min(vs):+7.0f}  n/run {len(vs)//runs}")


def bench(runs=60):
    print(f"STRATEGY BENCH — {runs} runs each, drift split up/down, rank out of 77\n")
    print(f"{'strategy':16}{'mean PnL':>9}{'med PnL':>9}{'mean rank':>10}{'top5%':>7}{'win%':>6}{'UP pnl':>8}{'DN pnl':>8}")
    print("-" * 73)
    for name, cls in SUTS:
        pnls = []; ranks = []; up = []; dn = []
        for r in range(runs):
            drift = 1 if r % 2 == 0 else -1
            mkt, lb, sut = run_market(r, drift, cls)
            n = len(lb)
            rank = next(i + 1 for i, (_, _, p) in enumerate(lb) if p is sut)
            pnl = sut.pnl(mkt.mark())
            pnls.append(pnl); ranks.append(rank)
            (up if drift == 1 else dn).append(pnl)
        top5 = 100 * sum(1 for x in ranks if x <= 5) / runs
        winr = 100 * sum(1 for x in pnls if x > 5) / runs
        print(f"{name:16}{st.mean(pnls):>9.0f}{st.median(pnls):>9.0f}{st.mean(ranks):>10.1f}"
              f"{top5:>6.0f}%{winr:>5.0f}%{st.mean(up):>8.0f}{st.mean(dn):>8.0f}")
    print("\nrank 1 = winner of 77. top5% = how often we land in prize seats. Robust = good in BOTH up & down.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "calibrate"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    if mode == "slide":
        slide_bench(n, naive_coin=float(sys.argv[3]) if len(sys.argv) > 3 else 0.0)
    else:
        (calibrate if mode == "calibrate" else bench)(n)
