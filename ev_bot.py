#!/usr/bin/env python3
"""
Delta Dash — EV-aware trading bot.

Model of the game: each instrument settles at base + Σ(realized random moves).
Slides reveal each move's *structure* ("+3d6"), never its outcome. So:
    fair  = base + Σ EV(revealed hints)
    risk  = Σ Var(revealed hints)   (uncertainty still on an open position)
The bot trades the gap between market mid and `fair`, sized down when `risk`
(per lot held to settlement) is large, and bounded by the position limit.

Hint feed (apply a revealed slide to an instrument):
  1. --hints-file PATH : operator appends lines  "<instrument_id> <expr>"
     e.g.  MKT1 3d6      MKT1 up by 2d6 - 4      RAIN coin:20
     (watched live; only new lines are applied)
  2. structured mock notifications carrying {"instrument_id","hint"} (testing)

`base` per instrument is captured from the first market mid seen (override with
--base IID=VALUE ...), because the real initial price is shown on the opening slide.

Usage:
  python3 ev_bot.py --url ws://localhost:8765/ws --alias evbot --hints-file hints.txt
  python3 ev_bot.py --url wss://delta-dash-eur.optibook.net/ws --game <id> --alias evbot --hints-file hints.txt
"""
from __future__ import annotations
import argparse, asyncio, base64, json, os, ssl, uuid
import websockets
import ev
try:
    import certifi; _CA = certifi.where()
except ImportError:
    _CA = None

LIMIT = 5


def sub(alias, password=""):
    return "basic." + base64.urlsafe_b64encode(f"{alias}:{password}".encode()).decode().rstrip("=")


class FairModel:
    """Tracks base + applied hints -> fair value and residual variance."""
    def __init__(self):
        self.base: dict[str, float] = {}
        self.sum_mean: dict[str, float] = {}
        self.sum_var: dict[str, float] = {}
        self.applied: dict[str, list[str]] = {}

    def set_base(self, iid, value):
        self.base.setdefault(iid, float(value))
        self.sum_mean.setdefault(iid, 0.0)
        self.sum_var.setdefault(iid, 0.0)
        self.applied.setdefault(iid, [])

    def apply_hint(self, iid, expr):
        self.set_base(iid, self.base.get(iid, 0.0))
        h = ev.parse(expr)
        self.sum_mean[iid] += h.mean
        self.sum_var[iid] += h.var
        self.applied[iid].append(expr.strip())
        print(f"  [hint] {iid} += {expr.strip()}  (EV {h.mean:+.3f}, var {h.var:.3f}) "
              f"=> fair {self.fair(iid):.3f}, std {self.std(iid):.3f}")

    def fair(self, iid):
        if iid not in self.base:
            return None
        return self.base[iid] + self.sum_mean.get(iid, 0.0)

    def std(self, iid):
        return self.sum_var.get(iid, 0.0) ** 0.5


class EVBot:
    def __init__(self, url, alias, game, size, take_ticks, hints_file):
        self.url, self.alias, self.game = url, alias, game
        self.size, self.take_ticks = size, take_ticks
        self.hints_file = hints_file
        self.uid = str(uuid.uuid4())
        self.model = FairModel()
        self.instruments = {}
        self.books = {}
        self.positions = {}
        self.my_orders = {}
        self.ws = None
        self._hint_pos = 0  # bytes read from hints file

    # ---- market helpers ----
    def best(self, iid, side):
        lst = self.books.get(iid, {}).get("bids" if side == "bid" else "asks", [])
        return lst[0]["price"] if lst else None

    def mid(self, iid):
        bb, ba = self.best(iid, "bid"), self.best(iid, "ask")
        if bb is not None and ba is not None: return (bb + ba) / 2
        return bb if bb is not None else ba

    def clamp(self, iid, price):
        inst = self.instruments[iid]; p = inst["parameters"]; tick = inst["tick_size"]
        price = round(price / tick) * tick
        return max(p["lower_bound"], min(p["upper_bound"], round(price, 6)))

    # ---- strategy ----
    def target_position(self, iid):
        """Desired signed position from edge vs fair, scaled by uncertainty."""
        fair = self.model.fair(iid)
        if fair is None: return 0
        m = self.mid(iid)
        if m is None: return 0
        tick = self.instruments[iid]["tick_size"]
        edge = fair - m                       # +ve => market cheap => want long
        # confidence shrinks as residual std grows (don't load risk on noise)
        std = max(tick, self.model.std(iid))
        # lots proportional to edge measured in "std units", capped at limit
        lots = edge / std
        target = max(-LIMIT, min(LIMIT, round(lots * LIMIT)))
        return int(target)

    async def send(self, type_, message):
        await self.ws.send(json.dumps({"type": type_, "message": message}))

    async def cancel_all(self, iid):
        for o in self.my_orders.get(iid, []):
            await self.send("delete_order", {"user_id": self.uid, "instrument_id": iid, "order_id": o["id"]})

    async def act(self, iid):
        inst = self.instruments.get(iid)
        if not inst or inst.get("expired") or inst.get("paused"): return
        fair = self.model.fair(iid)
        if fair is None: return
        tick = inst["tick_size"]
        pos = self.positions.get(iid, 0)
        target = self.target_position(iid)
        bb, ba = self.best(iid, "bid"), self.best(iid, "ask")
        await self.cancel_all(iid)

        # 1) TAKE: if the book is dislocated past fair by take_ticks, cross to move toward target.
        if target > pos and ba is not None and ba <= fair - self.take_ticks * tick and pos < LIMIT:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "bid", "price": self.clamp(iid, ba), "volume": min(self.size, target - pos)})
        elif target < pos and bb is not None and bb >= fair + self.take_ticks * tick and pos > -LIMIT:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "ask", "price": self.clamp(iid, bb), "volume": min(self.size, pos - target)})

        # 2) MAKE: rest quotes straddling fair, inventory-skewed, within limit.
        skew = (pos / LIMIT) * tick
        bid = self.clamp(iid, fair - tick - skew)
        ask = self.clamp(iid, fair + tick - skew)
        if pos < LIMIT and bid < fair:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "bid", "price": bid, "volume": self.size})
        if pos > -LIMIT and ask > fair:
            await self.send("insert_order", {"user_id": self.uid, "instrument_id": iid,
                            "side": "ask", "price": ask, "volume": self.size})

    # ---- ingest ----
    def ingest_shared(self, m):
        for i in m.get("instruments", []):
            self.instruments[i["id"]] = i
        for ob in m.get("order_books", []):
            self.books[ob["instrument_id"]] = {"bids": ob["bids"], "asks": ob["asks"]}
            iid = ob["instrument_id"]
            if iid not in self.model.base and self.mid(iid) is not None:
                self.model.set_base(iid, self.mid(iid))   # base = opening mid unless overridden

    def ingest_user(self, m):
        self.positions = {p["instrument_id"]: p["position"] for p in m.get("positions", [])}
        self.my_orders = {}
        for o in m.get("orders", []):
            self.my_orders.setdefault(o["instrument_id"], []).append(o)

    def handle_notification(self, m):
        msg = m.get("message", m)
        iid, hint = msg.get("instrument_id"), msg.get("hint")
        if iid and hint:
            self.model.apply_hint(iid, hint)

    def poll_hints_file(self):
        if not self.hints_file or not os.path.exists(self.hints_file): return
        with open(self.hints_file) as f:
            f.seek(self._hint_pos)
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        self.model.apply_hint(parts[0], parts[1])
            self._hint_pos = f.tell()

    async def hint_watcher(self):
        while True:
            await asyncio.sleep(0.5)
            try: self.poll_hints_file()
            except Exception as e: print("  [hint-file error]", e)

    async def run(self):
        ctx = ssl.create_default_context(cafile=_CA) if self.url.startswith("wss://") else None
        async with websockets.connect(self.url, subprotocols=[sub(self.alias)], ssl=ctx) as ws:
            self.ws = ws
            await self.send("connect", {"id": self.uid, "username": self.alias, "game_id": self.game})
            print(f"[*] EV bot {self.alias} ({self.uid[:8]}) -> {self.url}")
            asyncio.create_task(self.hint_watcher())
            async for raw in ws:
                try: m = json.loads(raw)
                except Exception: continue
                t = m.get("type")
                if t in ("shared_update", None):
                    self.ingest_shared(m)
                    for iid in list(self.instruments):
                        if not self.instruments[iid].get("expired"):
                            await self.act(iid)
                elif t == "user_update":
                    self.ingest_user(m)
                elif t in ("notification",):
                    self.handle_notification(m)
                elif t == "error":
                    print("  [error]", m.get("error"))


def main():
    ap = argparse.ArgumentParser(description="Delta Dash EV-aware bot")
    ap.add_argument("--url", default="ws://localhost:8765/ws")
    ap.add_argument("--alias", default="evbot")
    ap.add_argument("--game", default="mock")
    ap.add_argument("--size", type=int, default=2)
    ap.add_argument("--take-ticks", type=float, default=1.0, help="min ticks of dislocation to cross the spread")
    ap.add_argument("--hints-file", default=None)
    ap.add_argument("--base", nargs="*", default=[], help="IID=VALUE overrides for opening price")
    a = ap.parse_args()
    bot = EVBot(a.url, a.alias, a.game, a.size, a.take_ticks, a.hints_file)
    for kv in a.base:
        iid, v = kv.split("="); bot.model.set_base(iid, float(v))
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\n[*] stopped")


if __name__ == "__main__":
    main()
