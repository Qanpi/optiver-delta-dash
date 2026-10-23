#!/usr/bin/env python3
"""
feeder_poc — simulation-only proof of concept for Strategy E (coordinated self-dealing)
in the Delta Dash zero-sum agent market.

What it measures: a group of controlled accounts transfers PnL into one "main" account by trading
amongst themselves, so main finishes high and the "feeder" accounts finish low. Because the game
is zero-sum, main's gain equals the feeders' loss; no value is created. The point of interest is
the WIN RATE (how often main finishes first) as a function of how many self-trades the ring does,
and the footprint that activity leaves on the public tape.

Run:
    python3 feeder_poc.py                 # headline win-rate table + one sample leaderboard
    python3 feeder_poc.py --dump out.jsonl  # also write a trade log for feeder_detect.py
"""
from __future__ import annotations
import argparse, json, statistics as st
import dd_engine as E
from dd_engine import Participant, LIMIT


class Main(Participant):
    """The account the ring funnels PnL into. It takes no initiative of its own; the feeders
    drive all of its trades, so its behaviour lives entirely in the Ring participants."""
    kind = "ring_main"


class Ring(Participant):
    """A controlled account acting on main's behalf. Each active step it checks whether there is
    room between the best bid and best ask; if so it has main briefly rest a one-lot order one tick
    inside the touch, trades against it itself, and has main cancel the remainder. The net effect is
    a transfer of the inside spread from this account to main, with main exposed for only an instant.
    Positions are unwound each round trip, so neither account's ±5 limit binds."""
    kind = "feeder"
    main: Main = None
    rate = 0.3          # probability this account acts on a given engine step
    counter = None      # shared dict: accumulates the ring's order count (for a throughput cap)
    cap = None          # max orders/min across the whole ring (None = unlimited)

    def on_step(self, mkt):
        if self.rng.random() > self.rate:
            return
        if self.cap is not None and self.counter["orders"] > self.cap * max(1.0, mkt.t / 60):
            return
        bb, ba = mkt.book.best_bid(), mkt.book.best_ask()
        if bb is None or ba is None or ba - bb < 3:
            # no room to sit strictly inside the stranger spread; trading now would just move the
            # outside market. The ring idles until the book is wide (typically right after events).
            return
        m = self.main
        inside_bid, inside_ask = bb + 1, ba - 1
        mkt.cancel_all(m)
        # unwind toward flat: if this account is long, sell to main; if short, buy from main.
        if self.pos >= 0 and m.pos < LIMIT and self.pos > -LIMIT:
            if mkt.insert(m, "bid", inside_bid, 1):
                mkt.insert(self, "ask", inside_bid, 1); self.counter["orders"] += 2
        if self.pos <= 0 and m.pos > -LIMIT and self.pos < LIMIT:
            if mkt.insert(m, "ask", inside_ask, 1):
                mkt.insert(self, "bid", inside_ask, 1); self.counter["orders"] += 2
        mkt.cancel_all(m)


def run_round(seed, drift, n_feeders=2, rate=0.3, cap=None, dump=None):
    mkt = E.Market(seed=seed, duration=1800.0, drift=drift)
    E.make_population(mkt)
    main = mkt.add(Main("ringMAIN", mkt.rng))
    counter = {"orders": 0}
    feeders = [mkt.add(type("F", (Ring,), dict(main=main, rate=rate, cap=cap, counter=counter))
                       (f"ringF{i+1}", mkt.rng)) for i in range(n_feeders)]
    mkt.run()
    lb = mkt.leaderboard()
    mark = mkt.mark()
    rank = next(i + 1 for i, (_, _, p) in enumerate(lb) if p is main)
    main.final_pnl = main.pnl(mark)
    if dump is not None:
        with open(dump, "w") as f:
            for tr in mkt.trades:
                f.write(json.dumps({"trade_id": tr["trade_id"], "t": round(tr["t"], 2),
                                    "price": tr["price"], "volume": tr["volume"],
                                    "aggressor_side": tr["aggressor_side"],
                                    "buyer": tr["buyer"], "seller": tr["seller"]}) + "\n")
            f.write(json.dumps({"leaderboard": [[n, p] for n, p, _ in lb]}) + "\n")
    return mkt, lb, main, feeders, rank


def table(runs=24):
    print("Strategy E win rate vs ring throughput (2 feeder accounts, sandbox sim, "
          f"{runs} rounds each, drift split up/down)\n")
    print(f"{'orders/min cap':>14} {'main PnL mean':>13} {'main PnL min':>12} "
          f"{'mean rank':>10} {'WIN (rank 1)':>13} {'top 5':>7}")
    for cap in (20, 40, 60, 120, 240, None):
        pnls = []; ranks = []
        for r in range(runs):
            _, _, main, _, rank = run_round(r, 1 if r % 2 == 0 else -1, cap=cap)
            pnls.append(main.final_pnl); ranks.append(rank)
        label = "unlimited" if cap is None else str(cap)
        print(f"{label:>14} {st.mean(pnls):>13.0f} {min(pnls):>12.0f} {st.mean(ranks):>10.1f} "
              f"{100*sum(x==1 for x in ranks)/runs:>12.0f}% {100*sum(x<=5 for x in ranks)/runs:>6.0f}%")
    print("\nPnL is a pure transfer: main's gain = the feeder accounts' combined loss (zero-sum).")
    print("Win rate is set by throughput, not by skill, direction, or the event outcomes.")


def sample(seed=0):
    mkt, lb, main, feeders, rank = run_round(seed, 1, cap=240)
    m = mkt.mark()
    print(f"\nSample round (seed {seed}, settle {mkt.settled}): final leaderboard, top 8 + the ring\n")
    ring = {main.name, *(f.name for f in feeders)}
    for i, (name, pnl, p) in enumerate(lb):
        tag = "  <== RING MAIN" if p is main else ("  <== feeder" if name in ring else "")
        if i < 8 or name in ring:
            print(f"  {i+1:3} {name:12} {pnl:+9.1f}{tag}")
    print(f"\n  ring main finished rank {rank} of {len(lb)}; "
          f"feeders at ranks {[i+1 for i,(n,_,_) in enumerate(lb) if n in ring and n != main.name]}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Strategy E (self-dealing ring) — sandbox sim PoC")
    ap.add_argument("--runs", type=int, default=24)
    ap.add_argument("--dump", default=None, help="write a trade log (JSONL) for feeder_detect.py")
    ap.add_argument("--seed", type=int, default=0, help="seed for the sample / dumped round")
    a = ap.parse_args()
    table(a.runs)
    sample(a.seed)
    if a.dump:
        run_round(a.seed, 1, cap=240, dump=a.dump)
        print(f"\ntrade log written to {a.dump} -- run: python3 feeder_detect.py {a.dump}")
