#!/usr/bin/env python3
"""
Delta Dash — HAND bot: the manual overshoot-fade playbook, mechanized.

This is NOT the fast market-maker. It is the slow, human-executable strategy
written out so we can measure it: what a person could actually do live if the
real bot is not allowed / the laptop dies.

Rules (all human-paced):
  - center = where the mid was resting ~`lookback` seconds ago (DYNAMIC, not a
    fixed 50 — that dynamic center is why this beats the naive 30/70 bot).
  - dev = mid - center.
  - FADE a stalled overshoot:
      dev >= +band AND price has stopped rising  -> SELL (fade the spike)
      dev <= -band AND price has stopped falling  -> BUY  (fade the dip)
    The "stopped moving" check is the whole trick: a big move that is STILL
    going is a real repricing, not an overshoot — don't fade it, you get run
    over (this is how the naive bot died).
  - UNWIND back toward flat as price returns to center (take the reversion).
  - Fade harder near the price bounds (highest-confidence trade).
  - Never exceed +/-limit. Throttle: at most one action per `throttle` seconds
    (a human cannot click faster, and this is what makes it realistic).

Time params are in seconds; `--speed` divides them so they line up with the
mock's compressed clock (DD_SPEED). band/stall are in PRICE points (speed-free).

Usage:
  python3 hand_bot.py --url ws://localhost:8765/ws --alias hand --speed 8
"""
from __future__ import annotations
import argparse, asyncio, base64, json, ssl, time, uuid, zlib
from collections import deque
import websockets
try:
    import certifi; _CA = certifi.where()
except ImportError:
    _CA = None

LIMIT = 5


def sub(alias, pw=""):
    return "basic." + base64.urlsafe_b64encode(f"{alias}:{pw}".encode()).decode().rstrip("=")


class HandBot:
    def __init__(self, url, alias, game, band, lookback, react, stall, throttle, size,
                 mode="take", spread=6.0):
        self.url, self.alias, self.game = url, alias, game
        self.band = band              # points above/below center to call it an overshoot
        self.lookback = lookback      # seconds back to read "center"
        self.react = react            # seconds window to judge "has it stopped moving"
        self.stall = stall            # points: moved less than this over `react` => stalled
        self.throttle = throttle      # min seconds between my actions (human speed)
        self.size = size
        self.mode = mode              # "take" = cross to fade; "make" = post resting quotes, earn spread
        self.spread = spread          # make mode: total width of our quote (points)
        self.uid = str(uuid.uuid4())
        self.instruments = {}
        self.books = {}
        self.positions = {}
        self.my_orders = {}           # iid -> list of our resting orders (make mode)
        self.hist = {}                # iid -> deque[(t, mid)]
        self.last_act = 0.0
        self.ws = None

    def best(self, iid, side):
        lst = self.books.get(iid, {}).get("bids" if side == "bid" else "asks", [])
        return lst[0].get("price") if lst and isinstance(lst[0], dict) else None

    def mid(self, iid):
        bb, ba = self.best(iid, "bid"), self.best(iid, "ask")
        if bb is not None and ba is not None:
            return (bb + ba) / 2
        return bb if bb is not None else ba

    def mid_at(self, iid, secs_ago):
        """Mid closest to `secs_ago` seconds in the past."""
        h = self.hist.get(iid)
        if not h:
            return None
        target = time.time() - secs_ago
        best = min(h, key=lambda tm: abs(tm[0] - target))
        return best[1]

    def clamp(self, iid, price):
        inst = self.instruments[iid]; p = inst["parameters"]; tick = inst["tick_size"]
        price = round(price / tick) * tick
        return max(p["lower_bound"], min(p["upper_bound"], round(price, 6)))

    async def buy(self, iid, vol):
        ba = self.best(iid, "ask")
        if ba is None or vol <= 0:
            return
        await self.ws.send(json.dumps({"type": "insert_order", "message": {
            "user_id": self.uid, "instrument_id": iid, "side": "bid",
            "price": self.clamp(iid, ba), "volume": vol}}))

    async def sell(self, iid, vol):
        bb = self.best(iid, "bid")
        if bb is None or vol <= 0:
            return
        await self.ws.send(json.dumps({"type": "insert_order", "message": {
            "user_id": self.uid, "instrument_id": iid, "side": "ask",
            "price": self.clamp(iid, bb), "volume": vol}}))

    async def cancel_all(self, iid):
        for o in self.my_orders.get(iid, []):
            if "id" in o:
                await self.ws.send(json.dumps({"type": "delete_order", "message": {
                    "user_id": self.uid, "instrument_id": iid, "order_id": o["id"]}}))

    async def post(self, iid, side, price, vol):
        if vol <= 0:
            return
        await self.ws.send(json.dumps({"type": "insert_order", "message": {
            "user_id": self.uid, "instrument_id": iid, "side": side,
            "price": self.clamp(iid, price), "volume": vol}}))

    async def act_make(self, iid, inst, m, center, pos):
        """Post resting quotes around a slow center, inventory-skewed. Re-quoted
        only at throttle speed -> realistic human staleness (and adverse selection)."""
        p = inst["parameters"]; lo, hi = p["lower_bound"], p["upper_bound"]
        tick = inst["tick_size"]
        await self.cancel_all(iid)
        skew = (pos / LIMIT) * (self.spread / 2)       # long -> shade both quotes down to sell
        bid = center - self.spread / 2 - skew
        ask = center + self.spread / 2 - skew
        if pos < LIMIT and bid < m:                    # don't cross the market
            await self.post(iid, "bid", bid, self.size)
        if pos > -LIMIT and ask > m:
            await self.post(iid, "ask", ask, self.size)

    async def act(self, iid):
        inst = self.instruments.get(iid)
        if not inst or inst.get("expired") or inst.get("paused"):
            return
        m = self.mid(iid)
        if m is None:
            return
        now = time.time()
        self.hist.setdefault(iid, deque(maxlen=400)).append((now, m))
        if now - self.last_act < self.throttle:       # human throttle
            return

        p = inst["parameters"]; lo, hi = p["lower_bound"], p["upper_bound"]
        pos = self.positions.get(iid, 0)
        center = self.mid_at(iid, self.lookback)
        past = self.mid_at(iid, self.react)
        if center is None or past is None:
            return

        if self.mode == "make":
            await self.act_make(iid, inst, m, center, pos)
            self.last_act = now
            return
        dev = m - center
        moving = abs(m - past)                         # how much it moved over `react`
        stalled = moving < self.stall

        acted = False
        # 1) fade a STALLED overshoot
        if dev >= self.band and stalled and pos > -LIMIT:
            await self.sell(iid, self.size); acted = True
        elif dev <= -self.band and stalled and pos < LIMIT:
            await self.buy(iid, self.size); acted = True
        # 2) extreme near a bound: fade hard even if still twitching
        elif m >= hi - self.band and pos > -LIMIT:
            await self.sell(iid, self.size); acted = True
        elif m <= lo + self.band and pos < LIMIT:
            await self.buy(iid, self.size); acted = True
        # 3) reverted toward center: unwind back toward flat (take the reversion)
        elif abs(dev) < self.band / 2 and pos != 0:
            if pos > 0:
                await self.sell(iid, min(self.size, pos)); acted = True
            else:
                await self.buy(iid, min(self.size, -pos)); acted = True

        if acted:
            self.last_act = now

    def ingest_shared(self, m):
        for i in m.get("instruments", []):
            if "id" in i:
                self.instruments[i["id"]] = i
        for ob in m.get("order_books", []):
            iid = ob.get("instrument_id")
            if iid is not None:
                self.books[iid] = {"bids": ob.get("bids", []), "asks": ob.get("asks", [])}

    def ingest_user(self, m):
        self.positions = {p["instrument_id"]: p.get("position", 0)
                          for p in m.get("positions", []) if "instrument_id" in p}
        self.my_orders = {}
        for o in m.get("orders", []):
            if "instrument_id" in o:
                self.my_orders.setdefault(o["instrument_id"], []).append(o)

    @staticmethod
    def decode(raw):
        if isinstance(raw, str):
            try: return json.loads(raw)
            except Exception: return None
        for w in (zlib.MAX_WBITS, -zlib.MAX_WBITS, zlib.MAX_WBITS | 16):
            try: return json.loads(zlib.decompress(raw, w).decode("utf-8", "replace"))
            except Exception: continue
        return None

    async def run(self):
        ctx = ssl.create_default_context(cafile=_CA) if self.url.startswith("wss://") else None
        async with websockets.connect(self.url, subprotocols=[sub(self.alias)], ssl=ctx) as ws:
            self.ws = ws
            await ws.send(json.dumps({"type": "connect", "message": {
                "id": self.uid, "username": self.alias, "game_id": self.game}}))
            print(f"[*] hand bot {self.alias} ({self.uid[:8]}) band={self.band} "
                  f"lookback={self.lookback:.2f}s throttle={self.throttle:.2f}s -> {self.url}")
            try:
                async for raw in ws:
                    m = self.decode(raw)
                    if not isinstance(m, dict):
                        continue
                    t = m.get("type")
                    if t in ("shared_update", None):
                        self.ingest_shared(m)
                        for iid in list(self.instruments):
                            if not self.instruments[iid].get("expired"):
                                await self.act(iid)
                    elif t == "user_update":
                        self.ingest_user(m)
            except websockets.exceptions.ConnectionClosed:
                pass


def main():
    ap = argparse.ArgumentParser(description="Delta Dash manual overshoot-fade playbook, mechanized")
    ap.add_argument("--url", default="ws://localhost:8765/ws")
    ap.add_argument("--alias", default="hand")
    ap.add_argument("--game", default="mock")
    ap.add_argument("--band", type=float, default=10.0, help="points past center to call an overshoot")
    ap.add_argument("--stall", type=float, default=2.0, help="points moved over react-window below which it's 'stalled'")
    ap.add_argument("--lookback", type=float, default=10.0, help="seconds back to read dynamic center")
    ap.add_argument("--react", type=float, default=3.0, help="seconds window to judge if it stopped moving")
    ap.add_argument("--throttle", type=float, default=2.0, help="min seconds between actions (human speed)")
    ap.add_argument("--size", type=int, default=2)
    ap.add_argument("--mode", choices=["take", "make"], default="take",
                    help="take=cross to fade; make=post resting quotes to earn spread")
    ap.add_argument("--spread", type=float, default=6.0, help="make mode: total quote width in points")
    ap.add_argument("--speed", type=float, default=1.0, help="divide time params by this to match mock DD_SPEED")
    a = ap.parse_args()
    s = max(a.speed, 1e-9)
    bot = HandBot(a.url, a.alias, a.game, a.band, a.lookback / s, a.react / s, a.stall,
                  a.throttle / s, a.size, mode=a.mode, spread=a.spread)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n[*] stopped")


if __name__ == "__main__":
    main()
