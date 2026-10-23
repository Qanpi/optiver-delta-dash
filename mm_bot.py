#!/usr/bin/env python3
"""
Delta Dash — hint-agnostic market-maker with adaptive anchor.

Trades purely on order-book information, never reads slides. Core idea:

  ref = adaptive EMA of mid.
    - normally SLOW  (alpha_slow): price spikes above ref -> our ask gets lifted
      (we fade the overshoot); dips below -> our bid gets hit (we buy the dip).
      => mean-reversion / overshoot-fade.
    - on a JUMP (|mid-ref| > jump_thresh*recent_vol): snap ref toward mid
      (alpha_fast) so we stop fighting a real repricing. => follow, avoid
      adverse selection from stale quotes.

Then quote bid/ask around ref, inventory-skewed, within the position limit,
clamped to price bounds; widen & size-up fades near the bounds (bounded
reversion is the highest-confidence trade). Also snipe crossed books and
clearly-stale resting orders.

Same connection path as the real /ws — swap --url for the live event.

Usage:
  python3 mm_bot.py --url ws://localhost:8765/ws --alias mmbot
"""
from __future__ import annotations
import argparse, asyncio, base64, json, ssl, time, uuid, zlib
from collections import deque, Counter
import websockets
try:
    import certifi; _CA = certifi.where()
except ImportError:
    _CA = None

LIMIT = 5


def sub(alias, pw=""):
    return "basic." + base64.urlsafe_b64encode(f"{alias}:{pw}".encode()).decode().rstrip("=")


class MMBot:
    def __init__(self, url, alias, game, size, edge, a_slow, a_fast, jump_k, take_ticks, lean,
                 dry=False, capture=None, max_loss=None, max_warn=10, flatten=True, nopx_kill=20,
                 password="", state_path=None, control_path=None, safe_edge=3.0, reconnect=True,
                 order_throttle=0.0):
        self.url, self.alias, self.game = url, alias, game
        self.password = password
        self.size, self.edge = size, edge
        self.a_slow, self.a_fast, self.jump_k, self.take_ticks = a_slow, a_fast, jump_k, take_ticks
        self.lean = lean              # 0 = flat MM; >0 = ride the trend directionally (win-seeking)
        self.dry = dry                # dry-run: compute intended orders but SEND NOTHING
        self.capture_path = capture   # optional JSONL of every raw frame for offline study
        self.capf = None
        self.frame_counts = Counter()
        self.warned = set()
        self.intended = Counter()     # intended order count by type, dry-run only
        # --- kill-switch (fail-safe; off in dry-run) ---
        self.max_loss = max_loss      # halt if mark-to-market PnL < -max_loss (None = off)
        self.max_warn = max_warn      # halt if schema warnings reach this many (0 = off)
        self.flatten = flatten        # on halt, cross out of inventory (else just stop quoting)
        self.nopx_kill = nopx_kill    # halt if an active instrument stays un-priceable this many frames
        self.killed = False
        self.kill_reason = None
        self.cash = 0.0               # running cash from inferred fills (for PnL proxy)
        self.prev_pos = {}
        self.nopx = Counter()
        # --- live supervision: telemetry out, control in, safe-mode, reconnect ---
        self.state_path = state_path      # bot writes rank/pnl/pos here for the watchdog
        self.control_path = control_path  # watchdog writes {lean,edge,size,safe,max_loss} here
        self.safe_edge = safe_edge        # wider quote used in safe mode (less adverse selection)
        self.reconnect = reconnect        # auto-reconnect on any drop (never end the game on a blip)
        self.safe = False                 # defensive: lean->0 + widen quotes, keep arb + snipe
        self.limit = LIMIT                # position limit; overridden from configuration.limit live
        self.vol_limit = 200              # order volume cap; overridden from configuration if present
        self.order_throttle = order_throttle   # min seconds between re-quote cycles per instrument (0=every frame)
        self.last_act_t = {}
        self._ctrl_mtime = 0.0
        self.rank = None                  # our live leaderboard rank
        self.n_players = None
        self.lb_pnl = None                # our pnl per the server leaderboard (authoritative)
        self.joined = False               # confirmed in the game (leaderboard or a fill)
        self.fills = 0                    # count of fill events (position changes)
        self.only = set()                 # if non-empty, trade ONLY these instrument ids (e.g. RAIN)
        self.skip_test = False            # if True, never trade instruments flagged is_test
        self.priced = set()               # instruments we've ever seen a mid for (for the un-priceable guard)
        self.side_bid = "bid"             # label the server expects for a buy order (hot-reloadable)
        self.side_ask = "ask"             # label for a sell order (flip to buy/sell live if rejected)
        self.errors = 0                   # server error frames seen (schema-mismatch alarm)
        self._schema_warned = set()
        self.lo_default = 0.0             # strict price caps (0..100) as a fallback if feed omits bounds
        self.hi_default = 100.0
        self.server_cash = None           # authoritative realized cash from the server (positions.cash)
        self.stop = False
        self.started = time.time()
        self.fair = {}                    # iid -> known fair value (hint); overrides the book anchor
        # --- 'copy' strategy (hint-free): follow the aggressive trades of the current top-K
        # leaderboard names (trade buyer/seller + leaderboard are public), cross to the full
        # limit in that direction, hold until an opposite signal, stay flat and quiet otherwise.
        self.strategy = "mm"              # mm (quote + snipe) | copy (follow leaders)
        self.copy_k = 1                   # follow the top-K aliases on the leaderboard
        self.budget = 6.0                 # max fill events per minute (anti-churn cap)
        self.lb_names = []                # leaderboard aliases, best first
        self.want = {}                    # iid -> +1 / -1 current copy direction (0 = none yet)
        self.last_tid = {}                # iid -> newest trade_id ingested
        self.signals = 0
        self.uid = str(uuid.uuid4())
        self.instruments = {}
        self.books = {}
        self.positions = {}
        self.my_orders = {}
        self.ref = {}                 # fast adaptive anchor per instrument
        self.ref_slow = {}            # slow anchor -> trend = ref - ref_slow
        self.mids = {}                # recent mid history per instrument
        self.ws = None

    def best(self, iid, side):
        lst = self.books.get(iid, {}).get("bids" if side == "bid" else "asks", [])
        return lst[0].get("price") if lst and isinstance(lst[0], dict) else None

    def mid(self, iid):
        bb, ba = self.best(iid, "bid"), self.best(iid, "ask")
        if bb is not None and ba is not None: return (bb + ba) / 2
        return bb if bb is not None else ba

    def recent_vol(self, iid):
        h = self.mids.get(iid)
        if not h or len(h) < 3:
            return self.instruments[iid]["tick_size"]
        diffs = [abs(h[i] - h[i-1]) for i in range(1, len(h))]
        return max(self.instruments[iid]["tick_size"], sum(diffs) / len(diffs))

    def update_ref(self, iid):
        m = self.mid(iid)
        if m is None: return
        self.mids.setdefault(iid, deque(maxlen=20)).append(m)
        if iid not in self.ref:
            self.ref[iid] = m; return
        gap = m - self.ref[iid]
        jump = abs(gap) > self.jump_k * self.recent_vol(iid)
        alpha = self.a_fast if jump else self.a_slow
        self.ref[iid] += alpha * gap
        self.ref_slow[iid] = self.ref_slow.get(iid, m) + 0.03 * (m - self.ref_slow.get(iid, m))

    def target_pos(self, iid):
        """Win-seeking directional target: lean into the trend (ref vs slow ref),
        scaled to the position limit. lean=0 => always flat (pure MM)."""
        if self.lean <= 0 or iid not in self.ref_slow:
            return 0
        tick = self.instruments[iid]["tick_size"]
        trend = (self.ref[iid] - self.ref_slow[iid]) / max(tick, self.recent_vol(iid))
        return int(max(-self.limit, min(self.limit, round(self.lean * trend * self.limit))))

    def clamp(self, iid, price):
        inst = self.instruments.get(iid) or {}
        p = inst.get("parameters") or {}
        tick = inst.get("tick_size") or 0.1
        price = round(round(price / tick) * tick, 6)
        # bounds from the feed, else the known strict caps (0..100) so we NEVER post out of range
        lo = p.get("lower_bound"); hi = p.get("upper_bound")
        lo = self.lo_default if lo is None else lo
        hi = self.hi_default if hi is None else hi
        return max(lo, min(hi, price))

    def bound_bias(self, iid):
        """Push ref toward the bounds' midpoint — extreme prices are likely wrong
        (bounded reversion). Returns a small signed nudge in price units."""
        inst = self.instruments[iid]; p = inst["parameters"]
        lo, hi = p["lower_bound"], p["upper_bound"]; midb = (lo + hi) / 2
        r = self.ref.get(iid, midb)
        frac = (r - midb) / ((hi - lo) / 2 + 1e-9)      # -1..1, how extreme
        return -frac * inst["tick_size"] * 2 * (frac ** 2)  # stronger near the edges

    async def send(self, t, m):
        # In dry-run, suppress order actions (but still send connect to get the feed).
        if self.dry and t in ("insert_order", "delete_order"):
            self.intended[t] += 1
            if self.capf:
                self.capf.write(json.dumps({"ts": time.time(), "intended": {"type": t, "message": m}}) + "\n")
            return
        await self.ws.send(json.dumps({"type": t, "message": m}))

    def validate(self, m):
        """Check an incoming frame against our assumed schema; warn once per issue.
        This is how the dry-run confirms protocol assumptions on the real feed."""
        t = m.get("type")
        self.frame_counts[t if t else "snapshot"] += 1

        def warn(k):
            if k not in self.warned:
                self.warned.add(k)
                print(f"  [SCHEMA WARN] {k}")

        if t in ("shared_update", None):
            for k in ("instruments", "order_books", "leaderboard"):
                if k not in m:
                    warn(f"shared_update missing '{k}'")
            for ob in (m.get("order_books") or [])[:1]:
                for k in ("instrument_id", "bids", "asks"):
                    if k not in ob:
                        warn(f"order_book missing '{k}'")
                for lvl in (ob.get("bids") or [])[:1]:
                    if "price" not in lvl:
                        warn("book level missing 'price'")
                    if "bid" not in lvl and "volume" not in lvl and "size" not in lvl:
                        warn(f"book level volume field unknown: keys={list(lvl)}")
            for inst in (m.get("instruments") or [])[:1]:
                for k in ("id", "tick_size", "parameters"):
                    if k not in inst:
                        warn(f"instrument missing '{k}'")
        elif t == "user_update":
            for k in ("positions", "orders"):
                if k not in m:
                    warn(f"user_update missing '{k}'")
            # real server nests positions [[{user_id,instrument_id,cash,volume}]]; flatten first.
            # Warn on key NAMES only — values (cash) change every frame and would otherwise
            # register as a fresh issue each time and trip the max-warn kill switch.
            raw = m.get("positions") or []
            if raw and isinstance(raw[0], list):
                raw = raw[0]
            for p in raw[:1]:
                if not isinstance(p, dict):
                    warn(f"position not an object: {type(p).__name__}")
                    continue
                if "instrument_id" not in p:
                    warn(f"position missing 'instrument_id': keys={sorted(p)}")
                if "volume" not in p and "position" not in p:
                    warn(f"position size field unknown: keys={sorted(p)}")
            for o in (m.get("orders") or [])[:1]:
                for k in ("id", "instrument_id", "side", "price"):
                    if k not in o:
                        warn(f"order missing '{k}': keys={list(o)}")
        elif t == "error":
            print(f"  [server error] {m.get('error')}")
        elif t == "message":
            ack = m.get("message") or {}
            if ack.get("success") is False:
                print(f"  [order rejected] {ack.get('description')}")
        elif t == "notification":
            pass
        else:
            warn(f"unknown frame type '{t}': keys={list(m)}")

    def summary(self):
        tag = "DRY-RUN" if self.dry else "LIVE"
        frames = dict(self.frame_counts)
        refs = {i: round(self.ref.get(i, 0), 2) for i in self.instruments}
        line = (f"  [{tag}] frames={frames} warnings={len(self.warned)} "
                f"pos={self.positions} ref={refs} pnl~{self.mark_pnl():+.0f}")
        if self.safe:
            line += " SAFE"
        if self.rank is not None:
            line += f" rank={self.rank}/{self.n_players}"
        if self.killed:
            line += f" KILLED({self.kill_reason})"
        if self.dry:
            line += f" intended={dict(self.intended)}"
        print(line)
        self.write_state()

    def write_state(self):
        """Emit telemetry for the watchdog (atomic replace)."""
        if not self.state_path:
            return
        st = {"ts": time.time(), "alias": self.alias, "uptime": round(time.time() - self.started, 1),
              "joined": self.joined, "fills": self.fills,
              "instruments": {i: {"is_test": inst.get("is_test"), "expired": inst.get("expired"),
                                  "mid": self.mid(i)} for i, inst in self.instruments.items()},
              "trading": sorted(i for i, inst in self.instruments.items() if self.should_trade(i, inst)),
              "rank": self.rank, "n_players": self.n_players,
              "lb_pnl": self.lb_pnl, "mark_pnl": round(self.mark_pnl(), 2),
              "pos": self.positions, "safe": self.safe, "killed": self.killed,
              "lean": self.lean, "edge": self.edge, "size": self.size, "warnings": len(self.warned)}
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f)
            import os; os.replace(tmp, self.state_path)
        except Exception as e:
            print(f"  [state write failed] {e}")

    def apply_control(self):
        """Hot-reload watchdog commands WITHOUT touching the connection."""
        if not self.control_path:
            return
        try:
            import os
            mt = os.path.getmtime(self.control_path)
        except OSError:
            return
        if mt <= self._ctrl_mtime:
            return
        self._ctrl_mtime = mt
        try:
            with open(self.control_path) as f:
                c = json.load(f)
        except Exception:
            return
        changed = []
        for k in ("lean", "edge", "size", "max_loss", "safe_edge", "order_throttle", "limit",
                  "a_slow", "a_fast", "jump_k", "take_ticks", "vol_limit", "nopx_kill",
                  "strategy", "copy_k", "budget"):
            if k in c and getattr(self, k) != c[k]:
                setattr(self, k, c[k]); changed.append(f"{k}={c[k]}")
        if "only" in c:
            new_only = set(c["only"]) if isinstance(c["only"], (list, tuple, set)) else (
                {c["only"]} if c["only"] else set())
            if new_only != self.only:
                self.only = new_only; changed.append(f"only={sorted(self.only)}")
        if "fair" in c:
            fv = c["fair"]
            self.fair = {k: float(v) for k, v in fv.items()} if isinstance(fv, dict) and fv else {}
            changed.append(f"fair={self.fair}")
        if "skip_test" in c and bool(c["skip_test"]) != self.skip_test:
            self.skip_test = bool(c["skip_test"]); changed.append(f"skip_test={self.skip_test}")
        if "safe" in c and bool(c["safe"]) != self.safe:
            self.safe = bool(c["safe"]); changed.append(f"safe={self.safe}")
        if c.get("stop"):
            self.stop = True; changed.append("stop")
        if changed:
            print(f"  [CONTROL] applied {', '.join(changed)}")

    async def cancel_all(self, iid):
        for o in self.my_orders.get(iid, []):
            oid = o.get("id") if isinstance(o, dict) else None
            if oid is None and isinstance(o, dict):
                oid = o.get("order_id") or o.get("orderId") or o.get("orderID")
            if oid is not None:
                await self.send("delete_order", {"user_id": self.uid, "instrument_id": iid, "order_id": oid})

    # ---- kill-switch ----
    def mark_pnl(self):
        """Mark-to-market PnL proxy: inferred cash + inventory marked at our anchor.
        A floor guard, not an exact accounting. Prefers the server's per-instrument cash
        (real fill prices) over cash inferred from position deltas at our anchor."""
        mtm = self.server_cash if self.server_cash is not None else self.cash
        for iid, pos in self.positions.items():
            px = self.ref.get(iid)
            if px is None:
                px = self.mid(iid)
            if px is not None:
                mtm += pos * px
        return mtm

    def pnl(self):
        """Authoritative PnL: the server leaderboard value when we have it, else
        the rough mark-to-market proxy. Decisions must prefer the real number —
        the proxy can be badly off and must never trip the kill-switch alone."""
        return self.lb_pnl if self.lb_pnl is not None else self.mark_pnl()

    def check_kill(self):
        if self.killed or self.dry:
            return
        reason = None
        if self.max_warn and len(self.warned) >= self.max_warn:
            reason = f"schema warnings >= {self.max_warn} (protocol mismatch)"
        elif self.max_loss is not None and self.lb_pnl is not None and self.lb_pnl < -self.max_loss:
            # only the authoritative server pnl can trip the loss floor
            reason = f"server PnL {self.lb_pnl:+.0f} < floor -{self.max_loss}"
        if reason:
            self.trip_kill(reason)

    def trip_kill(self, reason):
        if self.killed:
            return
        self.killed = True
        self.kill_reason = reason
        act = "flatten + stop quoting" if self.flatten else "stop quoting (hold inventory)"
        print(f"  [KILL-SWITCH] {reason} -- {act}")

    async def flatten_pos(self, iid):
        """Cross the book to drive this instrument's position back to 0."""
        pos = self.positions.get(iid, 0)
        if pos == 0:
            return
        bb, ba = self.best(iid, "bid"), self.best(iid, "ask")
        vol = min(abs(pos), self.vol_limit)
        if pos > 0 and bb is not None:      # long -> sell into the bid
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "ask", "price": self.clamp(iid, bb), "volume": vol})
        elif pos < 0 and ba is not None:    # short -> buy from the ask
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "bid", "price": self.clamp(iid, ba), "volume": vol})

    def should_trade(self, iid, inst):
        """Which instruments we actually trade. With two instruments live (a test one
        and RAIN), we want RAIN only. `only` pins specific ids; else optionally skip
        is_test; always skip expired/paused."""
        if not inst or inst.get("expired") or inst.get("paused"):
            return False
        if self.only:
            return iid in self.only
        if self.skip_test and inst.get("is_test"):
            return False
        return True

    async def act(self, iid):
        inst = self.instruments.get(iid)
        if not self.should_trade(iid, inst): return
        if self.killed:                       # halted: pull quotes, optionally flatten
            await self.cancel_all(iid)
            if self.flatten:
                await self.flatten_pos(iid)
            return
        self.update_ref(iid)              # always refresh anchor (keeps jump detection live)
        if iid not in self.ref: return
        if self.strategy == "copy":
            await self.act_copy(iid, inst)
            return
        # order-throttle: cap re-quote rate per instrument (0 = every frame). Anchor still
        # updates above; we just leave resting orders in place between re-quotes.
        now = time.time()
        if self.order_throttle > 0 and now - self.last_act_t.get(iid, 0) < self.order_throttle:
            return
        self.last_act_t[iid] = now
        tick = inst.get("tick_size") or 0.1
        pos = self.positions.get(iid, 0)
        edge = self.safe_edge if self.safe else self.edge
        if iid in self.fair:
            # KNOWN fair value (hint) overrides the book anchor: quote around fair,
            # snipe anything on the wrong side, lean to the limit toward fair.
            ref = self.fair[iid]
            m0 = self.mid(iid)
            if m0 is None: m0 = ref
            tgt = -self.limit if m0 > ref else (self.limit if m0 < ref else 0)
        else:
            ref = self.ref[iid] + self.bound_bias(iid)
            tgt = 0 if self.safe else self.target_pos(iid)
        bb, ba = self.best(iid, "bid"), self.best(iid, "ask")
        await self.cancel_all(iid)

        # 1) crossed-book arb: buy ask & sell bid when locked/crossed by laggards
        if bb is not None and ba is not None and bb >= ba:
            if pos < self.limit:
                await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                                "side": "bid", "price": self.clamp(iid, ba), "volume": self.size})
            if pos > -self.limit:
                await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                                "side": "ask", "price": self.clamp(iid, bb), "volume": self.size})
            return

        # 2) snipe clearly-stale quotes vs ref
        if ba is not None and ba <= ref - self.take_ticks * tick and pos < self.limit:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "bid", "price": self.clamp(iid, ba), "volume": self.size})
        if bb is not None and bb >= ref + self.take_ticks * tick and pos > -self.limit:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "ask", "price": self.clamp(iid, bb), "volume": self.size})

        # 2b) lean-take: trend-follow toward the directional target by crossing at ~ref
        if tgt > pos and ba is not None and ba <= ref + tick and pos < self.limit:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "bid", "price": self.clamp(iid, ba), "volume": min(self.size, tgt - pos)})
        if tgt < pos and bb is not None and bb >= ref - tick and pos > -self.limit:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "ask", "price": self.clamp(iid, bb), "volume": min(self.size, pos - tgt)})

        # 3) make around ref, skewed toward the TARGET inventory (flat if lean=0 / safe)
        skew = ((pos - tgt) / self.limit) * tick
        bid = self.clamp(iid, ref - edge * tick - skew)
        ask = self.clamp(iid, ref + edge * tick - skew)
        if pos < self.limit and bid < ref:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "bid", "price": bid, "volume": self.size})
        if pos > -self.limit and ask > ref:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "ask", "price": ask, "volume": self.size})

    async def act_copy(self, iid, inst):
        """Hint-free copy strategy: drive position to +/-limit in the direction of the last
        aggressive trade by a current top-K leader, crossing the touch `size` lots per frame.
        No quotes at rest (nothing to be picked off), hard cap on fill rate (no churn)."""
        want = self.want.get(iid, 0)
        if want == 0 or self.safe:
            await self.cancel_all(iid)
            return
        elapsed_min = max(1.0, (time.time() - self.started) / 60)
        if self.budget and self.fills > self.budget * elapsed_min + 5:
            return                        # over budget: hold what we have, stop trading
        pos = self.positions.get(iid, 0)
        tgt = want * self.limit
        if pos == tgt:
            return
        bb, ba = self.best(iid, "bid"), self.best(iid, "ask")
        if tgt > pos and ba is not None:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "bid", "price": self.clamp(iid, ba), "volume": min(self.size, tgt - pos)})
        elif tgt < pos and bb is not None:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "ask", "price": self.clamp(iid, bb), "volume": min(self.size, pos - tgt)})

    def ingest_trades(self, trades):
        """Public prints carry buyer/seller aliases. A print whose aggressor is a current
        top-K leader (not us) sets the copy direction for that instrument."""
        if not isinstance(trades, list) or not self.lb_names:
            return
        top = set(self.lb_names[:self.copy_k]) - {self.alias}
        for t in trades:
            if not isinstance(t, dict):
                continue
            iid, tid = t.get("instrument_id"), t.get("trade_id")
            if iid is None or (tid is not None and tid <= self.last_tid.get(iid, -1)):
                continue
            if tid is not None:
                self.last_tid[iid] = tid
            side = t.get("aggressor_side")
            ag = t.get("buyer") if side == "bid" else t.get("seller") if side == "ask" else None
            if ag in top:
                new = 1 if side == "bid" else -1
                if new != self.want.get(iid, 0):
                    print(f"  [COPY] {iid} leader {ag} {'BUY' if new > 0 else 'SELL'} @{t.get('price')} -> go {'LONG' if new > 0 else 'SHORT'}")
                self.want[iid] = new; self.signals += 1

    def parse_leaderboard(self, lb):
        """Leaderboard array (lives in shared_update). Rows are [alias, pnl] or dicts."""
        if not isinstance(lb, list) or not lb:
            return
        self.n_players = len(lb)
        def name(r):
            if isinstance(r, (list, tuple)) and r: return r[0]
            if isinstance(r, dict): return r.get("alias") or r.get("username") or r.get("name")
        def pnl(r):
            if isinstance(r, (list, tuple)) and len(r) >= 2: return r[1]
            if isinstance(r, dict):
                for k in ("pnl", "score", "profit", "value"):
                    if k in r: return r[k]
        self.lb_names = [name(r) for r in lb]
        for i, row in enumerate(lb):
            if name(row) == self.alias:
                self.lb_pnl = pnl(row)
                if self.rank is None:
                    self.rank = i + 1
                break

    def ingest_shared(self, m):
        for i in m.get("instruments", []):
            if "id" in i:
                self.instruments[i["id"]] = i
        for ob in m.get("order_books", []):
            iid = ob.get("instrument_id")
            if iid is not None:
                self.books[iid] = {"bids": ob.get("bids", []), "asks": ob.get("asks", [])}
        self.parse_leaderboard(m.get("leaderboard"))
        self.ingest_trades(m.get("trades"))

    def ingest_user(self, m):
        # read live config (position + volume limits) if the server sends it
        cfg = m.get("configuration")
        if isinstance(cfg, dict):
            lim = cfg.get("limit")
            if isinstance(lim, (int, float)) and lim > 0:
                self.limit = int(lim)
            vl = cfg.get("orderVolumeLimit") or cfg.get("order_volume_limit")
            if isinstance(vl, (int, float)) and vl > 0:
                self.vol_limit = int(vl)
        # positions: real server nests as [[{...}]] with field "volume" (+ per-instrument "cash");
        # mock is a flat [{instrument_id, position}]. Handle both.
        raw = m.get("positions") or []
        if raw and isinstance(raw[0], list):
            raw = raw[0]
        new_pos = {}
        server_cash = 0.0; have_cash = False
        for p in raw:
            if isinstance(p, dict) and "instrument_id" in p:
                v = p.get("volume", p.get("position", 0))
                new_pos[p["instrument_id"]] = v
                if "cash" in p:
                    server_cash += p["cash"]; have_cash = True
        if have_cash:
            self.server_cash = server_cash   # authoritative realized cash from the server
        # infer fills from position deltas, mark cash at our anchor (PnL proxy for the floor guard)
        keys = set(new_pos) | set(self.prev_pos)
        for iid in keys:
            dp = new_pos.get(iid, 0) - self.prev_pos.get(iid, 0)
            if dp:
                self.fills += 1               # a fill happened (proof we're trading live)
                px = self.ref.get(iid)
                if px is None:
                    px = self.mid(iid)
                if px is not None:
                    self.cash -= dp * px      # buy (dp>0) spends; sell (dp<0) adds
        self.prev_pos = dict(new_pos)
        self.positions = new_pos
        self.my_orders = {}
        for o in m.get("orders", []):
            if "instrument_id" in o:
                self.my_orders.setdefault(o["instrument_id"], []).append(o)
        # rank arrives as leaderboard_position (user_update); the leaderboard ARRAY (for pnl)
        # lives in shared_update. Parse whichever is present here.
        if m.get("leaderboard_position") is not None:
            self.rank = m.get("leaderboard_position")
        self.parse_leaderboard(m.get("leaderboard"))
        # one-time JOIN confirmation: we're in the game if we show on the leaderboard
        # (registered) or we've had a fill / hold a position (actively trading).
        if not self.joined and (self.rank is not None or self.lb_pnl is not None
                                or self.fills > 0 or any(self.positions.values())):
            self.joined = True
            print(f"  [JOINED] in game as '{self.alias}' -- rank={self.rank}/{self.n_players} "
                  f"fills={self.fills} pos={self.positions}")

    @staticmethod
    def decode(raw):
        """Real feed sends JSON text OR zlib-compressed binary; handle both."""
        if isinstance(raw, str):
            try: return json.loads(raw)
            except Exception: return None
        for w in (zlib.MAX_WBITS, -zlib.MAX_WBITS, zlib.MAX_WBITS | 16):
            try: return json.loads(zlib.decompress(raw, w).decode("utf-8", "replace"))
            except Exception: continue
        return None

    async def run(self):
        """Reconnect loop: a dropped socket never ends the game — we reconnect and
        resume (position is server-side, so it survives). Only self.stop ends it."""
        if self.capture_path:
            self.capf = open(self.capture_path, "a")
        backoff = 1.0
        while not self.stop:
            try:
                await self._session()
                if self.stop:
                    break
                print("  [conn] feed ended; reconnecting...")
            except (websockets.exceptions.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                print(f"  [conn] lost ({type(e).__name__}: {e}); reconnecting in {backoff:.0f}s...")
            except Exception as e:
                print(f"  [conn] error ({type(e).__name__}: {e}); reconnecting in {backoff:.0f}s...")
            self.ws = None
            if self.stop or not self.reconnect:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)   # cap backoff; keep retrying for the whole game
        print("[*] run loop ended")

    async def _session(self):
        ctx = ssl.create_default_context(cafile=_CA) if self.url.startswith("wss://") else None
        async with websockets.connect(self.url, subprotocols=[sub(self.alias, self.password)],
                                      ssl=ctx, open_timeout=15, close_timeout=5) as ws:
            self.ws = ws
            await self.send("connect", {"id": self.uid, "username": self.alias, "game_id": self.game})
            mode = "DRY-RUN (no orders sent)" if self.dry else "LIVE (sending orders)"
            print(f"[*] MM bot {self.alias} ({self.uid[:8]}) -> {self.url}  [{mode}]"
                  f"{' [reconnect]' if self.reconnect else ''}")
            n = 0
            async for raw in ws:
                if self.stop:
                    return
                m = self.decode(raw)
                if not isinstance(m, dict):
                    continue
                if self.capf:
                    self.capf.write(json.dumps({"ts": time.time(), "frame": m}) + "\n"); self.capf.flush()
                try:
                    self.validate(m)
                    self.apply_control()       # hot-reload watchdog commands each frame
                    t = m.get("type")
                    if t in ("shared_update", None):
                        self.ingest_shared(m)
                        for iid in list(self.instruments):
                            inst = self.instruments[iid]
                            if not self.should_trade(iid, inst):
                                continue
                            # un-priceable guard: only for an instrument that WAS priced and
                            # then went dark — never for a just-opened one with no book yet.
                            if self.mid(iid) is not None:
                                self.priced.add(iid); self.nopx[iid] = 0
                            elif not inst.get("paused") and iid in self.priced:
                                self.nopx[iid] += 1
                                if not self.dry and self.nopx_kill and self.nopx[iid] >= self.nopx_kill:
                                    self.trip_kill(f"{iid} un-priceable {self.nopx[iid]} frames")
                            await self.act(iid)
                        self.check_kill()
                    elif t == "user_update":
                        self.ingest_user(m)
                        self.check_kill()
                except websockets.exceptions.ConnectionClosed:
                    raise                      # let the reconnect loop handle it
                except Exception as e:
                    self._frame_errors = getattr(self, "_frame_errors", 0) + 1
                    if self._frame_errors <= 5 or self._frame_errors % 50 == 0:
                        print(f"  [frame error #{self._frame_errors}] {type(e).__name__}: {e} "
                              f"(skipped; bot continues)")
                n += 1
                if n % 20 == 0:
                    self.summary()


def main():
    ap = argparse.ArgumentParser(description="Delta Dash hint-agnostic market maker")
    ap.add_argument("--url", default="ws://localhost:8765/ws")
    ap.add_argument("--alias", default="mmbot")
    ap.add_argument("--game", default="mock")
    ap.add_argument("--password", default="", help="ws auth password; empty for the alias-only join")
    ap.add_argument("--size", type=int, default=2)
    ap.add_argument("--edge", type=float, default=1.0, help="ticks from ref to rest quotes")
    ap.add_argument("--a-slow", type=float, default=0.05, help="anchor EMA alpha in calm")
    ap.add_argument("--a-fast", type=float, default=0.6, help="anchor EMA alpha on a jump")
    ap.add_argument("--jump-k", type=float, default=3.0, help="jump = gap > k * recent avg move")
    ap.add_argument("--take-ticks", type=float, default=1.0, help="min dislocation vs ref to snipe")
    ap.add_argument("--lean", type=float, default=0.0, help="0=flat MM; >0 rides the trend for win-rate")
    ap.add_argument("--dry-run", action="store_true", help="observe + validate + log intended orders, SEND NOTHING")
    ap.add_argument("--capture", default=None, help="JSONL path to log every raw frame + intended order")
    ap.add_argument("--max-loss", type=float, default=None, help="kill-switch: halt if mark PnL drops below -this (off by default)")
    ap.add_argument("--max-warn", type=int, default=10, help="kill-switch: halt after this many schema warnings (0=off)")
    ap.add_argument("--nopx-kill", type=int, default=20, help="kill-switch: halt if an active instrument is un-priceable this many frames (0=off)")
    ap.add_argument("--no-flatten", action="store_true", help="on kill, just stop quoting instead of crossing out of inventory")
    ap.add_argument("--state-file", default=None, help="write live telemetry (rank/pnl/pos) here for the watchdog")
    ap.add_argument("--control-file", default=None, help="hot-reload {lean,edge,size,safe,max_loss} from here, no reconnect")
    ap.add_argument("--safe-edge", type=float, default=3.0, help="quote width (ticks) in safe mode")
    ap.add_argument("--order-throttle", type=float, default=0.0, help="min seconds between re-quote cycles per instrument (0=every frame; raise if the server rate-limits)")
    ap.add_argument("--only", nargs="*", default=[], help="trade ONLY these instrument ids (e.g. RAIN); default = all active")
    ap.add_argument("--skip-test", action="store_true", help="never trade instruments flagged is_test")
    ap.add_argument("--uid", default=None, help="reuse a fixed user id (resume an existing alias session)")
    ap.add_argument("--fair", nargs="*", default=[], help="known fair values, e.g. RAIN=24")
    ap.add_argument("--no-reconnect", action="store_true", help="do NOT auto-reconnect on a dropped socket")
    ap.add_argument("--strategy", choices=["mm", "copy"], default="mm",
                    help="mm = quote+snipe market maker; copy = hint-free follow-the-leaders")
    ap.add_argument("--copy-k", type=int, default=1, help="copy: follow the top-K leaderboard aliases")
    ap.add_argument("--budget", type=float, default=6.0, help="copy: max fill events per minute (0=off)")
    a = ap.parse_args()
    bot = MMBot(a.url, a.alias, a.game, a.size, a.edge, a.a_slow, a.a_fast, a.jump_k, a.take_ticks, a.lean,
                dry=a.dry_run, capture=a.capture, max_loss=a.max_loss, max_warn=a.max_warn,
                flatten=not a.no_flatten, nopx_kill=a.nopx_kill, password=a.password,
                state_path=a.state_file, control_path=a.control_file, safe_edge=a.safe_edge,
                reconnect=not a.no_reconnect, order_throttle=a.order_throttle)
    bot.only = set(a.only)
    bot.skip_test = a.skip_test
    bot.strategy, bot.copy_k, bot.budget = a.strategy, a.copy_k, a.budget
    if a.uid:
        bot.uid = a.uid
    for kv in a.fair:
        if "=" in kv:
            k, v = kv.split("=", 1)
            try: bot.fair[k] = float(v)
            except ValueError: pass
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n[*] stopped")


if __name__ == "__main__":
    main()
