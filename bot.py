#!/usr/bin/env python3
"""
Delta Dash trading bot.

Connects to a Delta Dash /ws exactly as the web app does, so the same code runs
against the local mock (ws://localhost:8765/ws) and the real event
(wss://delta-dash-eur.optibook.net/ws) — only --url changes.

Strategy (starter: fair-value market maker):
  - Estimate fair value per instrument from the order-book mid (fallback: last
    trade, else mid of bounds).
  - Quote a bid and ask `edge` ticks around fair, sized `quote_size`.
  - Respect position limit: skew/skip the side that would breach it; lean quotes
    to mean-revert inventory back toward flat.
  - Re-quote each book update; cancel stale orders first.

This is a scaffold to iterate on, not a tuned strategy. Swap `fair_value` /
`desired_quotes` for your edge.

Requires: pip install websockets certifi
Usage:
  python3 bot.py --url ws://localhost:8765/ws --alias mybot
  python3 bot.py --url wss://delta-dash-eur.optibook.net/ws --game <exchange_id> --alias mybot
"""
from __future__ import annotations
import argparse, asyncio, base64, json, ssl, uuid
import websockets
try:
    import certifi; _CA = certifi.where()
except ImportError:
    _CA = None

LIMIT = 5


def sub(alias, password=""):
    return "basic." + base64.urlsafe_b64encode(f"{alias}:{password}".encode()).decode().rstrip("=")


class Bot:
    def __init__(self, url, alias, game, edge, size):
        self.url, self.alias, self.game = url, alias, game
        self.edge, self.size = edge, size
        self.uid = str(uuid.uuid4())
        self.instruments = {}
        self.books = {}            # iid -> {"bids":[...], "asks":[...]}
        self.positions = {}        # iid -> net
        self.my_orders = {}        # iid -> list[{id,side,price,volume}]
        self.last_trade = {}       # iid -> price
        self.ws = None

    # ---- market view ----
    def best(self, iid, side):
        lst = self.books.get(iid, {}).get("bids" if side == "bid" else "asks", [])
        return lst[0]["price"] if lst else None

    def fair_value(self, iid):
        bb, ba = self.best(iid, "bid"), self.best(iid, "ask")
        if bb is not None and ba is not None:
            return (bb + ba) / 2
        if iid in self.last_trade:
            return self.last_trade[iid]
        p = self.instruments[iid]["parameters"]
        return (p["lower_bound"] + p["upper_bound"]) / 2

    def clamp(self, iid, price):
        inst = self.instruments[iid]; p = inst["parameters"]; tick = inst["tick_size"]
        price = round(price / tick) * tick
        return max(p["lower_bound"], min(p["upper_bound"], round(price, 6)))

    def desired_quotes(self, iid):
        """Return (bid_price, ask_price) or None per side, inventory-aware."""
        inst = self.instruments[iid]
        if inst.get("expired") or inst.get("paused"):
            return None, None
        tick = inst["tick_size"]
        fair = self.fair_value(iid)
        pos = self.positions.get(iid, 0)
        # lean: shift quotes against inventory to revert toward flat
        skew = (pos / LIMIT) * tick
        bid = self.clamp(iid, fair - self.edge * tick - skew)
        ask = self.clamp(iid, fair + self.edge * tick - skew)
        # respect position limit: don't add to a maxed side
        if pos >= LIMIT: bid = None
        if pos <= -LIMIT: ask = None
        if bid is not None and ask is not None and bid >= ask:
            bid = self.clamp(iid, ask - tick)
        return bid, ask

    # ---- actions ----
    async def send(self, type_, message):
        await self.ws.send(json.dumps({"type": type_, "message": message}))

    async def cancel_all(self, iid):
        for o in self.my_orders.get(iid, []):
            await self.send("delete_order", {"user_id": self.uid, "instrument_id": iid, "order_id": o["id"]})

    async def requote(self, iid):
        bid, ask = self.desired_quotes(iid)
        await self.cancel_all(iid)
        if bid is not None:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "bid", "price": bid, "volume": self.size})
        if ask is not None:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "ask", "price": ask, "volume": self.size})

    # ---- event handling ----
    def ingest_shared(self, m):
        for i in m.get("instruments", []):
            self.instruments[i["id"]] = i
        for ob in m.get("order_books", []):
            self.books[ob["instrument_id"]] = {"bids": ob["bids"], "asks": ob["asks"]}
        for t in m.get("trades", []):
            self.last_trade[t.get("instrument_id")] = t.get("price")

    def ingest_user(self, m):
        self.positions = {p["instrument_id"]: p["position"] for p in m.get("positions", [])}
        self.my_orders = {}
        for o in m.get("orders", []):
            self.my_orders.setdefault(o["instrument_id"], []).append(o)
        rank = m.get("leaderboard_position")
        if rank:
            print(f"  [rank {rank}] positions={self.positions}")

    async def run(self):
        ctx = None
        if self.url.startswith("wss://"):
            ctx = ssl.create_default_context(cafile=_CA)
        async with websockets.connect(self.url, subprotocols=[sub(self.alias)], ssl=ctx) as ws:
            self.ws = ws
            await self.send("connect", {"id": self.uid, "username": self.alias, "game_id": self.game})
            print(f"[*] connected as {self.alias} ({self.uid[:8]}) -> {self.url}")
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                t = m.get("type")
                if t in ("shared_update", None):     # None = full snapshot
                    self.ingest_shared(m)
                    for iid in list(self.instruments):
                        if not self.instruments[iid].get("expired"):
                            await self.requote(iid)
                elif t == "user_update":
                    self.ingest_user(m)
                elif t == "error":
                    print("  [error]", m.get("error"))


def main():
    ap = argparse.ArgumentParser(description="Delta Dash trading bot")
    ap.add_argument("--url", default="ws://localhost:8765/ws")
    ap.add_argument("--alias", default="claudebot")
    ap.add_argument("--game", default="mock", help="exchange_id (any value for the mock)")
    ap.add_argument("--edge", type=int, default=2, help="ticks from fair to quote")
    ap.add_argument("--size", type=int, default=1, help="quote size")
    a = ap.parse_args()
    try:
        asyncio.run(Bot(a.url, a.alias, a.game, a.edge, a.size).run())
    except KeyboardInterrupt:
        print("\n[*] stopped")


if __name__ == "__main__":
    main()
