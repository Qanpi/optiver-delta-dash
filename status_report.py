#!/usr/bin/env python3
"""
One-shot live status line for relaying to the user's phone during the event.
Reads the bot's telemetry, diffs against the previous report, prints a compact
line showing how we're doing AND what changed since last time.

  python3 status_report.py <alias>

Run it periodically (e.g. every 30-60s) while the bot trades; relay its output.
"""
import json, os, sys, time

alias = sys.argv[1] if len(sys.argv) > 1 else "mmbot"
state = f"/tmp/dd_state_{alias}.json"
prev_path = f"/tmp/dd_report_prev_{alias}.json"


def load(p):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


st = load(state)
if not st:
    print(f"[status] no telemetry yet for '{alias}' (bot not up / not writing {state})")
    sys.exit(0)
prev = load(prev_path) or {}


def arrow(cur, old, good_up=True):
    if old is None or cur is None or cur == old:
        return ""
    up = cur > old
    sign = "+" if up else ""
    better = (up == good_up)
    return f" ({sign}{cur - old:g}{'↑' if up else '↓'})"


rank, n = st.get("rank"), st.get("n_players")
pnl = st.get("lb_pnl")
pnl = pnl if pnl is not None else st.get("mark_pnl")
fills = st.get("fills", 0)
trading = st.get("trading") or []
mode = "SAFE+CUT" if (st.get("safe") and st.get("size", 2) < 2) else "SAFE" if st.get("safe") else "normal"

# deltas since last report
d_rank = arrow(rank, prev.get("rank"), good_up=False) if rank is not None else ""
d_pnl = arrow(round(pnl, 0) if pnl is not None else None, prev.get("pnl")) if pnl is not None else ""
d_fills = fills - prev.get("fills", 0)

if not st.get("joined"):
    head = "CONNECTED, not yet on leaderboard"
elif not trading:
    head = "JOINED, idle (waiting for RAIN / only test open)"
else:
    head = f"TRADING {','.join(trading)}"

rank_s = f"rank {rank}/{n}{d_rank}" if rank is not None else "rank n/a"
pnl_s = f"PnL {pnl:+.0f}{d_pnl}" if pnl is not None else "PnL n/a"
line = (f"[{time.strftime('%H:%M:%S')}] {head} | {rank_s} | {pnl_s} | "
        f"trades {fills} (+{d_fills} since last) | pos {st.get('pos')} | {mode}")
if st.get("killed"):
    line += "  ** KILL-SWITCH TRIPPED **"
print(line)

# save snapshot for next diff
try:
    with open(prev_path, "w") as f:
        json.dump({"rank": rank, "pnl": round(pnl, 0) if pnl is not None else None, "fills": fills}, f)
except Exception:
    pass
