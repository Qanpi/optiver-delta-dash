#!/usr/bin/env python3
"""
Delta Dash watchdog — supervises a running mm_bot WITHOUT ever touching its
WebSocket. It reads the bot's telemetry file, and when it has sustained
evidence that we are *not winning and bleeding*, it writes a control file the
bot hot-reloads in-process (no reconnect, no dropped game).

Design principles (per the user's ask):
  - Act ONLY on sufficient, sustained evidence — never on one noisy sample.
  - The only connection-affecting actor is the bot; the watchdog just writes a
    small JSON command file. A watchdog crash cannot drop the game.
  - Every decision is logged and pushed to a human-readable status file.
  - Defensive only: it de-risks (safe mode -> smaller size). It never adds risk
    and never invents new strategies live.

Escalation ladder (each step needs the bleed to persist):
  1. SAFE MODE  — lean->0, widen quotes; keep structural edges (arb + snipe).
  2. CUT SIZE   — halve order size to shrink exposure if still bleeding.
It does not flip back and forth: once defensive, it stays defensive (a trading
game is short; thrashing config is worse than committing).

Usage (live, real-time defaults):
  python3 supervisor.py --state-file /tmp/dd_state.json --control-file /tmp/dd_ctrl.json
"""
from __future__ import annotations
import argparse, json, os, time
from collections import deque


class Watchdog:
    def __init__(self, a):
        self.sf, self.cf = a.state_file, a.control_file
        self.log_path, self.status_path = a.log, a.status
        self.poll = a.poll
        self.warmup = a.warmup
        self.window = a.window
        self.drop = a.drop
        self.confirm = a.confirm
        self.safe_edge = a.safe_edge
        self.min_size = a.min_size
        self.hist = deque(maxlen=4000)   # (ts, pnl, rank, n)
        self.level = 0                   # 0 normal, 1 safe, 2 safe+cut
        self.bad_streak = 0
        self.ctrl = {}                   # current command we've written

    # ---- io ----
    def read_state(self):
        try:
            with open(self.sf) as f:
                return json.load(f)
        except Exception:
            return None

    def write_ctrl(self, **kw):
        self.ctrl.update(kw)
        tmp = self.cf + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.ctrl, f)
        os.replace(tmp, self.cf)

    def pnl_of(self, st):
        # authoritative only: the server leaderboard pnl. The mark proxy is for
        # display, never decisions (it can be badly off).
        return st.get("lb_pnl")

    def note(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        if self.log_path:
            try:
                with open(self.log_path, "a") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    def status(self, st, verdict):
        if not self.status_path:
            return
        pnl = st.get("lb_pnl")
        if pnl is None:
            pnl = st.get("mark_pnl") or 0
        txt = (f"alias={st.get('alias')}  rank={st.get('rank')}/{st.get('n_players')}  "
               f"pnl={pnl:+.0f}  pos={st.get('pos')}  mode="
               f"{'SAFE+CUT' if self.level==2 else 'SAFE' if self.level==1 else 'normal'}  "
               f"uptime={st.get('uptime')}s\n{verdict}\n")
        try:
            tmp = self.status_path + ".tmp"
            with open(tmp, "w") as f:
                f.write(txt)
            os.replace(tmp, self.status_path)
        except Exception:
            pass

    # ---- the rule ----
    def bleeding(self, st):
        """True iff: losing money now, falling over the window, and ranked in the
        bottom part of the field. All three required -> 'not winning and bleeding'."""
        pnl = self.pnl_of(st)
        rank, n = st.get("rank"), st.get("n_players")
        if pnl is None:
            return False, "no pnl yet"
        now = time.time()
        self.hist.append((now, pnl, rank, n))
        if st.get("uptime", 0) < self.warmup:
            return False, f"warmup ({st.get('uptime')}s < {self.warmup}s)"
        past = [p for (t, p, r, nn) in self.hist if t <= now - self.window]
        if not past:
            return False, "not enough history yet"
        delta = pnl - past[-1]
        rank_bad = (rank is not None and n) and rank > max(1, n * 0.5)
        losing = pnl < 0
        falling = delta <= -self.drop
        why = f"Δpnl({self.window}s)={delta:+.0f} rank={rank}/{n} pnl={pnl:+.0f}"
        return (losing and falling and rank_bad), why

    def step(self):
        st = self.read_state()
        if not st:
            return
        bad, why = self.bleeding(st)
        if bad:
            self.bad_streak += 1
        else:
            self.bad_streak = 0
        verdict = ""
        if bad and self.bad_streak >= self.confirm and self.level < 2:
            if self.level == 0:
                self.level = 1
                self.write_ctrl(safe=True, edge=self.safe_edge)
                verdict = f"DEFENSIVE #1: SAFE mode (flat + widen to edge {self.safe_edge}). {why}"
                self.note(verdict)
            elif self.level == 1:
                self.level = 2
                new_size = max(self.min_size, int(st.get("size", 2)) // 2 or self.min_size)
                self.write_ctrl(safe=True, edge=self.safe_edge, size=new_size)
                verdict = f"DEFENSIVE #2: still bleeding in SAFE -> cut size to {new_size}. {why}"
                self.note(verdict)
            self.bad_streak = 0
        else:
            mode = "SAFE+CUT" if self.level == 2 else "SAFE" if self.level == 1 else "normal"
            verdict = f"OK ({mode}): {why}"
        self.status(st, verdict)

    def run(self):
        self.note(f"watchdog up. poll={self.poll}s warmup={self.warmup}s window={self.window}s "
                  f"drop={self.drop} confirm={self.confirm}")
        while True:
            try:
                self.step()
            except Exception as e:
                self.note(f"watchdog step error (non-fatal): {type(e).__name__}: {e}")
            time.sleep(self.poll)


def main():
    ap = argparse.ArgumentParser(description="Delta Dash mm_bot watchdog (defensive, connection-safe)")
    ap.add_argument("--state-file", required=True)
    ap.add_argument("--control-file", required=True)
    ap.add_argument("--log", default="/tmp/dd_watchdog.log")
    ap.add_argument("--status", default="/tmp/dd_status.txt")
    ap.add_argument("--poll", type=float, default=3.0, help="seconds between checks")
    ap.add_argument("--warmup", type=float, default=30.0, help="ignore the first N seconds of the game")
    ap.add_argument("--window", type=float, default=45.0, help="look-back window for the pnl trend")
    ap.add_argument("--drop", type=float, default=40.0, help="pnl fall over the window that counts as bleeding")
    ap.add_argument("--confirm", type=int, default=2, help="consecutive bad checks before acting (hysteresis)")
    ap.add_argument("--safe-edge", type=float, default=3.0, help="quote width to command in safe mode")
    ap.add_argument("--min-size", type=int, default=1)
    a = ap.parse_args()
    Watchdog(a).run()


if __name__ == "__main__":
    main()
