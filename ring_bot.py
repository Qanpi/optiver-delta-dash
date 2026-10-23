#!/usr/bin/env python3
"""
ring_bot — Strategy E live runner: one MAIN alias plus N FEEDER aliases trading through each
other so PnL moves from the feeders into main. Permitted by the event rules, which allow
"trading collusions across actors". Sandbox study and win-rate curve: feeder_poc.py.

Mechanism (same as feeder_poc.Ring, made safe for a real network):
  1. Read the latest RAIN book. Act only if best_ask - best_bid >= --min-gap (default 3), so
     there is a tick strictly inside the spread that strangers are not on.
  2. MAIN rests 1 lot one tick inside the touch (bid at bb+1, or ask at ba-1) and waits for
     the server ack carrying its order_id.
  3. A FEEDER sends the opposite 1 lot at the same price, which matches MAIN's order first
     (price priority over the stranger touch).
  4. Cleanup: delete MAIN's remainder and any FEEDER remainder (a stranger got there first).
  Legs alternate so feeders unwind toward flat; MAIN's ring inventory stays within ±1-2.
  Per round trip MAIN gains about (spread - 2) ticks and the feeders lose the same.

All sockets live in one asyncio loop, so the legs are strictly sequenced: MAIN's ack is
received before the feeder order is sent.

Run against the mock first:
  DD_SPEED=5 DD_DURATION=1800 DD_BATCH=1 python3 mock_server_v2.py
  python3 ring_bot.py --url ws://localhost:8765/ws --main me --feeders f1 f2

Live: fixed uids per alias (--uid-file), --game <exchange_id>, start with --dry-run.
"""
from __future__ import annotations
import argparse, asyncio, json, ssl, time, uuid
from collections import deque
import websockets
from mm_bot import sub, MMBot, _CA

LIMIT = 5


class Conn:
    """One alias' socket: book/positions/orders from its frames, acks in FIFO order."""

    def __init__(self, url, alias, uid, game, password="", log=None):
        self.url, self.alias, self.uid, self.game, self.password = url, alias, uid, game, password
        self.ws = None; self.ready = asyncio.Event()
        self.acks: asyncio.Queue = asyncio.Queue()
        self.pos = {}; self.orders = {}; self.cash = None; self.limit = LIMIT
        self.books = {}; self.leaderboard = []; self.rank = None
        self.sent = deque()          # send timestamps, for the per-alias rate cap
        self.log = log

    async def run(self, stop):
        ctx = ssl.create_default_context(cafile=_CA) if self.url.startswith("wss://") else None
        while not stop.is_set():
            try:
                async with websockets.connect(self.url, subprotocols=[sub(self.alias, self.password)],
                                              ssl=ctx, open_timeout=15, close_timeout=5,
                                              max_size=None) as ws:
                    self.ws = ws
                    await self.send("connect", {"id": self.uid, "username": self.alias,
                                                "game_id": self.game})
                    async for raw in ws:
                        m = MMBot.decode(raw)
                        if isinstance(m, dict):
                            self.ingest(m)
            except Exception as e:
                print(f"  [{self.alias}] connection lost ({type(e).__name__}: {e}); retrying")
            self.ws = None; self.ready.clear()
            if not stop.is_set():
                await asyncio.sleep(1.0)

    async def send(self, t, m):
        self.sent.append(time.time())
        if self.log:
            self.log.write(json.dumps({"ts": time.time(), "alias": self.alias, "out": t, "m": m}) + "\n")
        await self.ws.send(json.dumps({"type": t, "message": m}))

    def ingest(self, m):
        t = m.get("type")
        if t in ("shared_update", None):
            for ob in m.get("order_books") or []:
                self.books[ob.get("instrument_id")] = ob
            if m.get("leaderboard"):
                self.leaderboard = m["leaderboard"]
        elif t == "user_update":
            cfg = m.get("configuration") or {}
            if isinstance(cfg.get("limit"), (int, float)) and cfg["limit"] > 0:
                self.limit = int(cfg["limit"])
            raw = m.get("positions") or []
            if raw and isinstance(raw[0], list):
                raw = raw[0]
            self.pos = {p["instrument_id"]: p.get("volume", p.get("position", 0))
                        for p in raw if isinstance(p, dict) and "instrument_id" in p}
            cash = [p["cash"] for p in raw if isinstance(p, dict) and "cash" in p]
            self.cash = sum(cash) if cash else None
            self.orders = {}
            for o in m.get("orders") or []:
                self.orders.setdefault(o.get("instrument_id"), []).append(o)
            if m.get("leaderboard_position") is not None:
                self.rank = m["leaderboard_position"]
            self.ready.set()
        elif t == "message":
            self.acks.put_nowait(m.get("message") or {})
        elif t == "error":
            print(f"  [{self.alias}] server error {m.get('error')}")
            if self.log:
                self.log.write(json.dumps({"ts": time.time(), "alias": self.alias, "error": m}) + "\n")

    async def request(self, t, m, timeout):
        """Send one order action and wait for its ack (acks are FIFO per socket)."""
        while not self.acks.empty():
            self.acks.get_nowait()                 # drop late acks from a timed-out request
        await self.send(t, m)
        try:
            return await asyncio.wait_for(self.acks.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def rate(self, window=60.0):
        cut = time.time() - window
        while self.sent and self.sent[0] < cut:
            self.sent.popleft()
        return len(self.sent)


def touch(ob):
    bids = [b["price"] for b in ob.get("bids", []) if b.get("bid", 0) > 0]
    asks = [a["price"] for a in ob.get("asks", []) if a.get("ask", 0) > 0]
    return (max(bids) if bids else None), (min(asks) if asks else None)


class Ring:
    def __init__(self, a, main, feeders):
        self.a, self.main, self.feeders = a, main, feeders
        self.trips = 0; self.legs = 0; self.misses = 0; self.fi = 0
        self.t0 = time.time()

    def pos(self, c):
        return c.pos.get(self.a.iid, 0)

    def pick(self):
        """Next feeder with room, round-robin. Returns (feeder, side MAIN rests) or None."""
        m = self.pos(self.main)
        for k in range(len(self.feeders)):
            f = self.feeders[(self.fi + k) % len(self.feeders)]
            if f.rate() >= self.a.max_rate or self.main.rate() >= self.a.max_rate:
                continue
            fp = self.pos(f)
            # unwind the feeder toward flat; break ties by unwinding MAIN toward flat
            want_main_buy = fp > 0 or (fp == 0 and m <= 0)
            cap = self.a.main_band
            if want_main_buy and m < cap and fp > -f.limit + 1:
                self.fi += k + 1; return f, "bid"
            if not want_main_buy and m > -cap and fp < f.limit - 1:
                self.fi += k + 1; return f, "ask"
        return None

    async def leg(self):
        ob = self.main.books.get(self.a.iid)
        if not ob:
            return
        bb, ba = touch(ob)
        if bb is None or ba is None or ba - bb < self.a.min_gap:
            return
        p = self.pick()
        if p is None:
            return
        f, side = p
        price = bb + 1 if side == "bid" else ba - 1
        iid, to = self.a.iid, self.a.ack_timeout
        if self.a.dry_run:
            print(f"  [dry] main {side} {price} <- {f.alias}  (book {bb}/{ba})")
            self.legs += 1; return
        ack = await self.main.request("insert_order", {"user_id": self.main.uid, "instrument_id": iid,
                                                       "side": side, "price": price, "volume": 1}, to)
        if not ack or not ack.get("success") or ack.get("order_id") is None:
            self.misses += 1; return       # rejected, timed out, or a stranger filled it instantly
        moid = ack["order_id"]
        fside = "ask" if side == "bid" else "bid"
        fack = await f.request("insert_order", {"user_id": f.uid, "instrument_id": iid,
                                                "side": fside, "price": price, "volume": 1}, to)
        # cleanup: delete MAIN's order (harmless "not found" if filled), and the feeder's if it rested
        await self.main.request("delete_order", {"user_id": self.main.uid, "instrument_id": iid,
                                                 "order_id": moid}, to)
        if fack and fack.get("order_id") is not None:
            self.misses += 1
            await f.request("delete_order", {"user_id": f.uid, "instrument_id": iid,
                                             "order_id": fack["order_id"]}, to)
        else:
            self.legs += 1

    def status(self):
        lb = {r[0]: r[1] for r in self.main.leaderboard if isinstance(r, (list, tuple)) and len(r) >= 2}
        mins = (time.time() - self.t0) / 60
        ring = [self.main] + self.feeders
        parts = "  ".join(f"{c.alias} pos {self.pos(c):+d} pnl {lb.get(c.alias, '?')}" for c in ring)
        print(f"[ring] {mins:5.1f}m legs {self.legs} ({self.legs / max(mins, 1e-9):.0f}/min) "
              f"misses {self.misses} | main rank {self.main.rank} | {parts}", flush=True)


async def flatten(c, iid, to):
    """Cancel everything for this alias; optionally cross out of inventory."""
    for o in c.orders.get(iid, []):
        if o.get("id") is not None:
            await c.request("delete_order", {"user_id": c.uid, "instrument_id": iid, "order_id": o["id"]}, to)


async def amain(a):
    uids = {}
    if a.uid_file:
        try:
            uids = json.load(open(a.uid_file))
        except FileNotFoundError:
            pass
    aliases = [a.main] + a.feeders
    for al in aliases:
        uids.setdefault(al, str(uuid.uuid4()))
    if a.uid_file:
        json.dump(uids, open(a.uid_file, "w"), indent=1)
    log = open(a.log, "a") if a.log else None
    stop = asyncio.Event()
    conns = [Conn(a.url, al, uids[al], a.game, a.password, log) for al in aliases]
    tasks = [asyncio.create_task(c.run(stop)) for c in conns]
    main, feeders = conns[0], conns[1:]
    await asyncio.wait_for(asyncio.gather(*(c.ready.wait() for c in conns)), 30)
    print(f"[*] ring up: main {a.main}, feeders {a.feeders}, {a.iid} min-gap {a.min_gap}, "
          f"max {a.max_rate}/min per alias{' [DRY-RUN]' if a.dry_run else ''}")
    ring = Ring(a, main, feeders)
    t_end = time.time() + a.duration if a.duration else None
    last = 0.0
    try:
        while not (t_end and time.time() > t_end):
            if all(c.ws is not None and c.ready.is_set() for c in conns):
                inst_ok = True
                ob = main.books.get(a.iid)
                if ob is None:
                    inst_ok = False
                if inst_ok:
                    try:
                        await ring.leg()
                    except websockets.exceptions.ConnectionClosed:
                        pass
            if time.time() - last > a.status_every:
                ring.status(); last = time.time()
            if log:
                log.flush()
            await asyncio.sleep(a.interval)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for c in conns:
            if c.ws is not None:
                try:
                    await flatten(c, a.iid, a.ack_timeout)
                except Exception:
                    pass
        ring.status()
        stop.set()
        for c in conns:
            if c.ws is not None:
                await c.ws.close()
        for t in tasks:
            t.cancel()


def main():
    ap = argparse.ArgumentParser(description="Strategy E ring: feeders transfer PnL into main")
    ap.add_argument("--url", default="ws://localhost:8765/ws")
    ap.add_argument("--game", default="mock", help="exchange_id (game_id) of the live event")
    ap.add_argument("--password", default="")
    ap.add_argument("--main", required=True, help="alias that receives the PnL")
    ap.add_argument("--feeders", nargs="+", required=True, help="aliases that pay it")
    ap.add_argument("--uid-file", default="ring_uids.json", help="fixed uid per alias (reuse on restart)")
    ap.add_argument("--iid", default="RAIN", help="instrument to run the ring on")
    ap.add_argument("--min-gap", type=float, default=3, help="min best_ask - best_bid to act (ticks)")
    ap.add_argument("--main-band", type=int, default=2, help="max |MAIN ring inventory|")
    ap.add_argument("--max-rate", type=int, default=300, help="max order actions/min per alias")
    ap.add_argument("--interval", type=float, default=0.05, help="seconds between leg attempts")
    ap.add_argument("--ack-timeout", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=0, help="stop after this many seconds (0 = run)")
    ap.add_argument("--status-every", type=float, default=10.0)
    ap.add_argument("--log", default=None, help="JSONL of every order action + errors")
    ap.add_argument("--dry-run", action="store_true", help="connect and print legs, send no orders")
    a = ap.parse_args()
    try:
        asyncio.run(amain(a))
    except KeyboardInterrupt:
        print("\n[*] stopped")


if __name__ == "__main__":
    main()
