#!/usr/bin/env python3
"""
feeder_detect — surveillance proof of concept: flag coordinated self-dealing (Strategy E rings)
from a public trade log alone.

This is the defensive counterpart to feeder_poc.py and the more useful assignment artefact: it
shows that the abuse, however profitable in theory, leaves an obvious footprint on the public tape
that an operator can catch with a few aggregate statistics — no private data required.

Input: a JSONL trade log, one trade per line with fields
    {trade_id, price, volume, aggressor_side, buyer, seller}
and optionally a final line {"leaderboard": [[alias, pnl], ...]}.
Accepts both feeder_poc.py dumps and the real exchange export (prices as float or string).

Signals, per directed pair (A funnels to B):
  1. Counterparty concentration — what share of B's trades are against A. A normal trader spreads
     across the field; a funnel is dominated by one name.
  2. PnL mirror — B's gain and A's loss are near-equal and opposite (only checkable with a
     leaderboard line; zero-sum pairs are the signature of a transfer, not of trading skill).
  3. Round-trip symmetry — A and B trade with each other in BOTH directions at near-equal volume,
     i.e. they pass inventory back and forth rather than accumulating a real position.

A pair tripping concentration + (mirror or symmetry) is reported as a suspected ring.

Run:  python3 feeder_detect.py trades.jsonl
"""
from __future__ import annotations
import json, sys, collections


def load(path):
    trades = []; lb = None
    for line in open(path):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if "leaderboard" in d:
            lb = {row[0]: row[1] for row in d["leaderboard"]}
            continue
        d["price"] = float(d["price"])
        trades.append(d)
    return trades, lb


def analyse(trades, lb=None, conc_th=0.5, min_trades=10):
    # per-account trade count, and directed pair counts / volumes
    n_trades = collections.Counter()
    pair_vol = collections.Counter()          # (X, Y) -> lots X traded with Y (either direction)
    pair_dir = collections.defaultdict(lambda: [0, 0])   # (X,Y) sorted -> [X->Y buys, Y->X buys]
    for t in trades:
        b, s, v = t["buyer"], t["seller"], t["volume"]
        n_trades[b] += v; n_trades[s] += v
        pair_vol[(b, s)] += v; pair_vol[(s, b)] += v
        key = tuple(sorted((b, s)))
        pair_dir[key][0 if b == key[0] else 1] += v

    findings = []
    seen = set()
    for (x, y), _ in pair_vol.most_common():
        key = tuple(sorted((x, y)))
        if key in seen:
            continue
        a, c = key
        shared = pair_dir[key][0] + pair_dir[key][1]
        # the pair itself must have enough shared activity to be worth judging — a one-off
        # back-and-forth of a couple of lots is noise, not a ring.
        if shared < min_trades:
            continue
        # concentration from each side's perspective
        conc_a = shared / max(1, n_trades[a])
        conc_c = shared / max(1, n_trades[c])
        conc = max(conc_a, conc_c)
        if conc < conc_th:
            continue
        seen.add(key)
        d0, d1 = pair_dir[key]
        symmetry = min(d0, d1) / max(1, max(d0, d1))      # 1.0 = perfectly balanced round trips
        mirror = None
        if lb and a in lb and c in lb:
            ga, gc = lb[a], lb[c]
            if ga * gc < 0:                                # opposite signs
                mirror = 1 - abs(ga + gc) / max(1, abs(ga) + abs(gc))   # 1.0 = exact mirror
        flag = conc >= conc_th and (symmetry >= 0.6 or (mirror is not None and mirror >= 0.6))
        findings.append(dict(pair=key, shared_lots=shared, conc=conc,
                             conc_a=conc_a, conc_c=conc_c, symmetry=symmetry, mirror=mirror,
                             lb_a=lb.get(a) if lb else None, lb_c=lb.get(c) if lb else None,
                             flag=flag))
    findings.sort(key=lambda f: (-f["flag"], -f["conc"]))
    return findings, n_trades


def report(path, conc_th=0.5):
    trades, lb = load(path)
    findings, n_trades = analyse(trades, lb, conc_th=conc_th)
    print(f"\nfeeder_detect — {path}")
    print(f"  {len(trades)} trades, {len(n_trades)} accounts"
          + (f", leaderboard present" if lb else ", no leaderboard line"))
    flagged = [f for f in findings if f["flag"]]
    if not flagged:
        print("  no account pair trips the ring thresholds — tape looks clean.\n")
    for f in flagged:
        a, c = f["pair"]
        print(f"\n  *** SUSPECTED RING: {a} <-> {c} ***")
        print(f"      {f['shared_lots']} lots traded between them")
        print(f"      concentration: {f['conc_a']*100:.0f}% of {a}'s volume / "
              f"{f['conc_c']*100:.0f}% of {c}'s volume is with each other")
        print(f"      round-trip symmetry: {f['symmetry']*100:.0f}% (100% = pure back-and-forth)")
        if f["mirror"] is not None:
            print(f"      PnL mirror: {f['mirror']*100:.0f}%  ({a} {f['lb_a']:+.0f} vs {c} {f['lb_c']:+.0f})")
    # also show the strongest non-flagged pair for context
    near = [f for f in findings if not f["flag"]]
    if near:
        f = near[0]; a, c = f["pair"]
        print(f"\n  (strongest non-flagged pair: {a}/{c}, concentration {f['conc']*100:.0f}%, "
              f"symmetry {f['symmetry']*100:.0f}% — below threshold)")
    print()
    return flagged


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python3 feeder_detect.py <trades.jsonl> [concentration_threshold]")
        sys.exit(1)
    th = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    report(sys.argv[1], conc_th=th)
