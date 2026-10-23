#!/usr/bin/env python3
"""
Delta Dash — NAIVE threshold bot (the "buy low / sell high by hand" strategy).

Dead simple, and executable by a human:
  - look at the mid price.
  - mid < LOW  -> buy  (build toward a long,  up to +limit)
  - mid > HIGH -> sell (build toward a short, down to -limit)
  - in between  -> do nothing.
Hold the position to settlement. The bet is pure mean-reversion: the value
wanders inside its [lower, upper] bounds, so "cheap" tends to rise and "dear"
tends to fall. No order book, no fair-value model, no spread capture.

This exists as a BASELINE to compare against the adaptive market-maker
(mm_bot.py). Same protocol / connection, so it runs drop-in on mock or live.

Usage:
  python3 naive_bot.py --url ws://localhost:8765/ws --alias naive --low 30 --high 70
"""
from __future__ import annotations
import argparse, asyncio, base64, json, ssl, uuid, zlib
import websockets
try:
    import certifi; _CA = certifi.where()
except ImportError:
    _CA = None

LIMIT = 5


def sub(alias, pw=""):
    return "basic." + base64.urlsafe_b64encode(f"{alias}:{pw}".encode()).decode().rstrip("=")


class NaiveBot:
    def __init__(self, url, alias, game, low, high, size):
        self.url, self.alias, self.game = url, alias, game
        self.low, self.high, self.size = low, high, size
        self.uid = str(uuid.uuid4())
        self.instruments = {}
        self.books = {}
        self.positions = {}
        self.ws = None

    def best(self, iid, side):
        lst = self.books.get(iid, {}).get("bids" if side == "bid" else "asks", [])
        return lst[0].get("price") if lst and isinstance(lst[0], dict) else None

    def mid(self, iid):
        bb, ba = self.best(iid, "bid"), self.best(iid, "ask")
        if bb is not None and ba is not None:
            return (bb + ba) / 2
        return bb if bb is not None else ba

    def clamp(self, iid, price):
        inst = self.instruments[iid]; p = inst["parameters"]; tick = inst["tick_size"]
        price = round(price / tick) * tick
        return max(p["lower_bound"], min(p["upper_bound"], round(price, 6)))

    async def buy(self, iid, vol):
        ba = self.best(iid, "ask")
        if ba is None:
            return
        await self.ws.send(json.dumps({"type": "insert_order", "message": {
            "user_id": self.uid, "instrument_id": iid, "side": "bid",
            "price": self.clamp(iid, ba), "volume": vol}}))

    async def sell(self, iid, vol):
        bb = self.best(iid, "bid")
        if bb is None:
            return
        await self.ws.send(json.dumps({"type": "insert_order", "message": {
            "user_id": self.uid, "instrument_id": iid, "side": "ask",
            "price": self.clamp(iid, bb), "volume": vol}}))

    async def act(self, iid):
        inst = self.instruments.get(iid)
        if not inst or inst.get("expired") or inst.get("paused"):
            return
        m = self.mid(iid)
        if m is None:
            return
        pos = self.positions.get(iid, 0)
        if m < self.low and pos < LIMIT:                 # cheap -> buy toward +limit
            await self.buy(iid, min(self.size, LIMIT - pos))
        elif m > self.high and pos > -LIMIT:             # dear -> sell toward -limit
            await self.sell(iid, min(self.size, LIMIT + pos))

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
            print(f"[*] naive bot {self.alias} ({self.uid[:8]}) low={self.low} high={self.high} -> {self.url}")
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
    ap = argparse.ArgumentParser(description="Delta Dash naive threshold bot (buy low / sell high)")
    ap.add_argument("--url", default="ws://localhost:8765/ws")
    ap.add_argument("--alias", default="naive")
    ap.add_argument("--game", default="mock")
    ap.add_argument("--low", type=float, default=30.0, help="buy when mid below this")
    ap.add_argument("--high", type=float, default=70.0, help="sell when mid above this")
    ap.add_argument("--size", type=int, default=2, help="units added per trigger")
    a = ap.parse_args()
    bot = NaiveBot(a.url, a.alias, a.game, a.low, a.high, a.size)
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n[*] stopped")


if __name__ == "__main__":
    main()
