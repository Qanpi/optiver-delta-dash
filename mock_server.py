#!/usr/bin/env python3
"""
Delta Dash — local mock exchange.

Speaks the reverse-engineered Delta Dash protocol so the bot can be developed
and backtested offline, with a *moving* market (real events are only
intermittently live). Seeded from a real captured order book
(`seed_book.json`) and driven by noise traders whose fair value drifts toward a
hidden settlement value — so the book moves and trades print.

Protocol parity (see memory `delta-dash-protocol`):
  - WS path /ws, subprotocol  basic.<base64url(alias:password)>
  - client -> {"type":"connect","message":{id,username,game_id}}
  - client -> {"type":"insert_order","message":{user_id,instrument_id,side,price,volume}}
  - client -> {"type":"delete_order","message":{user_id,instrument_id,order_id}}
  - server -> shared_update {instruments, order_books, trades, leaderboard}
  - server -> user_update   {configuration, orders, positions, leaderboard_position, leaderboard, trades}
  - server -> error {error:{code}}

NOTE: `trades`, `positions`, `orders` wire shapes are mock approximations where
the real shape was not observed; adapt when a live trade frame is captured.

Run:  python3 mock_server.py           # ws://localhost:8765/ws
Point the bot at ws://localhost:8765/ws (no TLS).
"""
from __future__ import annotations
import asyncio, base64, itertools, json, os, random, re, time, uuid
import websockets
import ev

HOST = "localhost"
PORT = int(os.environ.get("DD_PORT", "8765"))
SPEED = float(os.environ.get("DD_SPEED", "1"))   # >1 compresses time for batch runs
TICK_BROADCAST = 0.5 / SPEED  # seconds between shared_update broadcasts
NOISE_INTERVAL = 0.25 / SPEED # seconds between noise-trader actions
POSITION_LIMIT = 5
ORDER_VOLUME_LIMIT = 200

# Hint-driven round: (delay_seconds, instrument_id, hint_expr). The mock draws
# the REALIZED outcome, moves the instrument's true settlement value, and
# broadcasts a structured notification carrying the hint. Real events run ~10-15
# hints; we use 15. Mix of up/down moves of varying size.
N_HINTS = 15
FIRST_HINT, HINT_GAP = 4 / SPEED, 5 / SPEED
# Net-ZERO-drift pool: every up-move mirrored by an equal down-move, so the
# truth is a driftless random walk. Prevents a secret bullish bias from making
# "lean long" look good for the wrong reason. (Override to a biased pool only
# deliberately.) Sum of EVs ~= 0.
_HINT_POOL = ["3d6", "-3d6", "2d6", "-2d6", "d6", "-d6",
              "coin:20", "-coin:20", "unifint:1:10", "-unifint:1:10"]


def make_schedule(n=N_HINTS):
    return [(FIRST_HINT + i * HINT_GAP, "MKT1", random.choice(_HINT_POOL)) for i in range(n)]


HINT_SCHEDULE = make_schedule()
SETTLE_AFTER = FIRST_HINT + N_HINTS * HINT_GAP + 8 / SPEED   # settle a bit after last hint
MARKET_LAG = 0.08             # how fast noise MMs chase the (overshot) target per tick
OVERSHOOT_DECAY = 0.90        # overshoot decays toward 0 each noise tick (reversion)
N_AGENTS = int(os.environ.get("DD_AGENTS", "16"))   # heterogeneous human-like traders

# Regime presets so we can STRESS-TEST a strategy against different behavioral
# assumptions, not just our default. Robust = wins across all, not max in one.
REGIME = os.environ.get("DD_REGIME", "mixed")
_REGIMES = {
    "mixed":     dict(overshoot=0.9, sharp=3, w=dict(chaser=3, contrarian=2, random=2, value=3, timid=2)),
    "lag":       dict(overshoot=0.2, sharp=2, w=dict(chaser=1, contrarian=1, random=2, value=4, timid=3)),
    "overshoot": dict(overshoot=1.6, sharp=2, w=dict(chaser=6, contrarian=1, random=2, value=1, timid=1)),
    "efficient": dict(overshoot=0.3, sharp=8, w=dict(chaser=1, contrarian=1, random=1, value=4, timid=1)),
    "random":    dict(overshoot=0.5, sharp=1, w=dict(chaser=2, contrarian=2, random=6, value=1, timid=2)),
}
RG = _REGIMES.get(REGIME, _REGIMES["mixed"])
OVERSHOOT_FRAC = RG["overshoot"]
N_SHARP = max(1, round(RG["sharp"] * N_AGENTS / 16))   # scale tough traders with field size


def make_agents(n):
    """A messy population: momentum chasers, contrarians, random traders, timid
    wide makers, occasional panickers — plus a few SHARP near-fair traders
    (the real top-10 we must beat). Random personalities => non-deterministic."""
    agents = []
    for i in range(n):
        if i < N_SHARP:
            kind = "sharp"
        else:
            kinds = ["chaser", "contrarian", "random", "value", "timid"]
            kind = random.choices(kinds, weights=[RG["w"][k] for k in kinds])[0]
        if kind == "sharp":
            a = {"name": f"p{i:02d}", "kind": kind, "value_weight": random.uniform(0.9, 1.0),
                 "overreact": random.uniform(-0.1, 0.1), "noise": random.uniform(0.2, 0.8),
                 "lag": random.uniform(0.45, 0.75), "spread": random.uniform(1, 2),
                 "size": random.randint(3, 6), "activity": random.uniform(0.6, 0.9),
                 "aggress": random.uniform(0.3, 0.6), "panic": 0.0, "mistake": random.uniform(0.0, 0.03)}
        else:
            a = {
                "name": f"p{i:02d}", "kind": kind,
                "value_weight": random.uniform(0.0, 0.25) if kind == "random" else random.uniform(0.4, 1.0),
                "overreact": {"chaser": random.uniform(0.5, 1.6), "contrarian": random.uniform(-0.8, -0.1),
                              "random": 0.0, "value": random.uniform(-0.1, 0.3),
                              "timid": random.uniform(0.0, 0.4)}[kind],
                "noise": random.uniform(0.5, 3.0), "lag": random.uniform(0.05, 0.6),
                "spread": random.uniform(4, 9) if kind == "timid" else random.uniform(1, 6),
                "size": random.randint(1, 6), "activity": random.uniform(0.2, 0.8),
                "aggress": random.uniform(0.1, 0.6), "panic": random.uniform(0.0, 0.03),
                "mistake": random.uniform(0.0, 0.15),
            }
        agents.append(a)
    return agents

_oid = itertools.count(1)
_DICE = re.compile(r"^(\d*)d(\d+)$", re.I)


def sample_realized(expr: str) -> float:
    """Draw one realized outcome for a hint expression (dice/const/+-combos)."""
    expr = expr.strip().lower()
    for noise in ("up by", "down by", "sum of", "moves", "move", "value", "the", "of"):
        expr = expr.replace(noise, " ")
    expr = expr.replace(" ", "")
    total, sign = 0.0, 1
    for t in re.findall(r"[+-]?[^+-]+", expr):
        s = -1 if t.startswith("-") else 1
        t = t.lstrip("+-")
        if not t:
            continue
        m = _DICE.match(t)
        if m:
            n = int(m.group(1) or "1"); sides = int(m.group(2))
            total += s * sum(random.randint(1, sides) for _ in range(n))
        elif t.startswith("coin:"):
            v = float(t.split(":")[1]); total += s * (v if random.random() < 0.5 else 0)
        elif t.startswith("unifint:"):
            _, a, b = t.split(":"); total += s * random.randint(int(a), int(b))
        else:
            try: total += s * float(t)
            except ValueError: pass
    return total


def load_seed():
    try:
        return json.load(open(f"{__file__.rsplit('/',1)[0]}/seed_book.json"))
    except Exception:
        return {"instruments": [{"id": "MKT1", "name": "MKT1", "tick_size": 0.1,
                "paused": False, "expired": False, "expiration_value": None,
                "is_test": True, "parameters": {"lower_bound": 0, "upper_bound": 100}}],
                "order_books": [{"instrument_id": "MKT1", "bids": [], "asks": []}]}


class Order:
    __slots__ = ("id", "user", "iid", "side", "price", "vol")
    def __init__(self, user, iid, side, price, vol):
        self.id = str(next(_oid)); self.user = user; self.iid = iid
        self.side = side; self.price = round(price, 6); self.vol = vol


class Book:
    def __init__(self, inst):
        self.inst = inst
        self.bids: list[Order] = []   # price desc, time asc
        self.asks: list[Order] = []   # price asc,  time asc

    def _sort(self):
        self.bids.sort(key=lambda o: -o.price)
        self.asks.sort(key=lambda o: o.price)

    def add(self, o: Order):
        (self.bids if o.side == "bid" else self.asks).append(o)
        self._sort()

    def remove(self, user, oid):
        for lst in (self.bids, self.asks):
            for i, o in enumerate(lst):
                if o.id == oid and o.user == user:
                    lst.pop(i); return True
        return False

    def best_bid(self): return self.bids[0].price if self.bids else None
    def best_ask(self): return self.asks[0].price if self.asks else None

    def match(self, taker: Order, room_fn):
        """Match taker against the book; return trade dicts. `room_fn(user, side)`
        gives how many lots `user` may still take on `side` without breaching the
        position limit; a maker with no room is cancelled (popped) rather than
        filled beyond the limit."""
        trades = []
        book = self.asks if taker.side == "bid" else self.bids
        maker_side = "ask" if taker.side == "bid" else "bid"
        def crosses(maker):
            return taker.price >= maker.price if taker.side == "bid" else taker.price <= maker.price
        while taker.vol > 0 and book and crosses(book[0]):
            maker = book[0]
            mroom = room_fn(maker.user, maker_side)
            if mroom <= 0:
                book.pop(0)                      # maker can't fill without breaching -> cancel
                continue
            q = min(taker.vol, maker.vol, mroom)
            px = maker.price
            trades.append({"instrument_id": self.inst["id"], "price": px, "volume": q,
                           "aggressor": taker.side, "buyer": taker.user if taker.side == "bid" else maker.user,
                           "seller": maker.user if taker.side == "bid" else taker.user,
                           "ts": time.time()})
            taker.vol -= q; maker.vol -= q
            if maker.vol == 0:
                book.pop(0)
        if taker.vol > 0:
            self.add(taker)
        return trades

    def aggregate(self):
        def agg(lst, key):
            out = {}
            for o in lst:
                out[o.price] = out.get(o.price, 0) + o.vol
            return [{key: v, "price": p} for p, v in sorted(out.items(),
                     key=lambda kv: -kv[0] if key == "bid" else kv[0])]
        return {"instrument_id": self.inst["id"], "bids": agg(self.bids, "bid"),
                "asks": agg(self.asks, "ask")}


class Exchange:
    def __init__(self):
        seed = load_seed()
        self.instruments = seed["instruments"]
        self.books = {i["id"]: Book(i) for i in self.instruments}
        self.tradeable = [i for i in self.instruments if not i.get("expired") and not i.get("paused")]
        # Hidden settlement value = base + Σ realized hint outcomes. Market makers
        # chase `truth` with lag, so EV-fair (base + Σ EV) is tradeable vs the lagging book.
        self.base_truth, self.truth, self.market_center = {}, {}, {}
        for i in self.tradeable:
            ob = next((o for o in seed["order_books"] if o["instrument_id"] == i["id"]), None)
            if ob and ob["bids"] and ob["asks"]:
                base = (ob["bids"][0]["price"] + ob["asks"][0]["price"]) / 2
            else:
                p = i["parameters"]; base = (p["lower_bound"] + p["upper_bound"]) / 2
            self.base_truth[i["id"]] = base
            self.truth[i["id"]] = base
            self.market_center[i["id"]] = base
        self.overshoot = {i["id"]: 0.0 for i in self.tradeable}  # transient crowd overreaction
        self.hint_queue = list(HINT_SCHEDULE)
        self.agents = make_agents(N_AGENTS)
        # each agent's private belief per instrument, seeded at base
        self.belief = {a["name"]: {i["id"]: self.base_truth[i["id"]] for i in self.tradeable}
                       for a in self.agents}
        # seed resting orders from aggregated seed book
        for ob in seed["order_books"]:
            b = self.books[ob["instrument_id"]]
            for lvl in ob["bids"]:
                b.add(Order("seed", ob["instrument_id"], "bid", lvl["price"], lvl["bid"]))
            for lvl in ob["asks"]:
                b.add(Order("seed", ob["instrument_id"], "ask", lvl["price"], lvl["ask"]))
        self.positions: dict[str, dict[str, int]] = {}   # user -> iid -> net
        self.realized: dict[str, float] = {}             # user -> realized cash
        self.aliases: dict[str, str] = {}                # user -> alias
        self.recent_trades: list[dict] = []
        self.clients: set = set()
        self.start = time.time()
        self.settled = False

    # ---- helpers ----
    def pos(self, user, iid): return self.positions.setdefault(user, {}).get(iid, 0)
    def room(self, user, iid, side):
        """Lots `user` may still take on `side` before hitting the position limit."""
        p = self.pos(user, iid)
        return (POSITION_LIMIT - p) if side == "bid" else (POSITION_LIMIT + p)
    def inst(self, iid): return next((i for i in self.instruments if i["id"] == iid), None)

    def valid_price(self, inst, price):
        p = inst["parameters"]
        if not (p["lower_bound"] <= price <= p["upper_bound"]):
            return False
        tick = inst["tick_size"]
        return abs(round(price / tick) * tick - price) < 1e-9

    def apply_fill(self, user, iid, side, price, vol):
        signed = vol if side == "bid" else -vol
        self.positions.setdefault(user, {})[iid] = self.pos(user, iid) + signed
        # cash: buying pays, selling receives
        self.realized[user] = self.realized.get(user, 0.0) + (-price*vol if side == "bid" else price*vol)

    def mark_pnl(self, user):
        cash = self.realized.get(user, 0.0)
        mtm = 0.0
        for iid, net in self.positions.get(user, {}).items():
            inst = self.inst(iid)
            if inst.get("expiration_value") is not None:
                ref = inst["expiration_value"]
            else:
                b = self.books[iid]; bb, ba = b.best_bid(), b.best_ask()
                ref = (bb+ba)/2 if bb is not None and ba is not None else (bb or ba or self.truth.get(iid, 0))
            mtm += net * ref
        return round(cash + mtm, 2)

    def leaderboard(self):
        users = set(self.realized) | set(self.positions) | set(self.aliases)
        rows = [[self.aliases.get(u, u), self.mark_pnl(u)] for u in users]
        rows.sort(key=lambda r: -r[1])
        return rows

    def insert_order(self, user, msg):
        iid, side = msg.get("instrument_id"), msg.get("side")
        price, vol = msg.get("price"), int(msg.get("volume", 1))
        inst = self.inst(iid)
        if inst is None or inst.get("expired") or inst.get("paused"):
            return {"type": "error", "error": {"code": "invalid_instrument"}}
        if side not in ("bid", "ask"):
            return {"type": "error", "error": {"code": "invalid_side"}}
        if not (1 <= vol <= ORDER_VOLUME_LIMIT):
            return {"type": "error", "error": {"code": "invalid_volume"}}
        if not self.valid_price(inst, price):
            return {"type": "error", "error": {"code": "invalid_price"}}
        # position-limit check: reject if already at the cap in that direction
        cur = self.pos(user, iid)
        if side == "bid" and cur >= POSITION_LIMIT:
            return {"type": "error", "error": {"code": "position_limit"}}
        if side == "ask" and cur <= -POSITION_LIMIT:
            return {"type": "error", "error": {"code": "position_limit"}}
        # cap fillable volume so a single order can't breach the limit
        room = POSITION_LIMIT - cur if side == "bid" else POSITION_LIMIT + cur
        vol = min(vol, max(0, room))
        if vol == 0:
            return {"type": "error", "error": {"code": "position_limit"}}
        taker = Order(user, iid, side, price, vol)
        trades = self.books[iid].match(taker, lambda u, s: self.room(u, iid, s))
        for t in trades:
            self.apply_fill(t["buyer"], iid, "bid", t["price"], t["volume"])
            self.apply_fill(t["seller"], iid, "ask", t["price"], t["volume"])
        self.recent_trades = (self.recent_trades + trades)[-100:]
        return None

    def settle(self):
        for inst in self.instruments:
            if inst.get("expiration_value") is None and inst["id"] in self.truth:
                inst["expiration_value"] = round(self.truth[inst["id"]], 2)
            inst["expired"] = True
        self.settled = True

    # ---- wire frames ----
    def shared_update(self):
        return {"type": "shared_update", "instruments": self.instruments,
                "order_books": [b.aggregate() for b in self.books.values()],
                "trades": self.recent_trades[-25:], "leaderboard": self.leaderboard()}

    def user_update(self, user):
        orders = []
        for b in self.books.values():
            for o in b.bids + b.asks:
                if o.user == user:
                    orders.append({"id": o.id, "instrument_id": o.iid, "side": o.side,
                                   "price": o.price, "volume": o.vol})
        positions = [{"instrument_id": iid, "position": net}
                     for iid, net in self.positions.get(user, {}).items()]
        lb = self.leaderboard()
        alias = self.aliases.get(user, user)
        rank = next((idx+1 for idx, r in enumerate(lb) if r[0] == alias), None)
        return {"type": "user_update", "configuration": {"limit": POSITION_LIMIT, "leaderboard_visible": True},
                "orders": orders, "positions": positions, "leaderboard_position": rank,
                "leaderboard": lb, "trades": self.recent_trades[-25:]}


EX = Exchange()


def parse_subprotocol(headers):
    raw = headers.get("Sec-WebSocket-Protocol", "")
    return raw.split(",")[0].strip() if raw else None


async def handler(ws):
    sub = ws.request.headers.get("Sec-WebSocket-Protocol", "").split(",")[0].strip()
    user = None
    EX.clients.add(ws)
    try:
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            t, msg = m.get("type"), m.get("message", {})
            if t == "connect":
                user = msg.get("id") or str(uuid.uuid4())
                EX.aliases[user] = msg.get("username", user)
                await ws.send(json.dumps(EX.shared_update()))
                await ws.send(json.dumps(EX.user_update(user)))
            elif t == "insert_order":
                err = EX.insert_order(msg.get("user_id", user), msg)
                print(f"  [ORDER] {EX.aliases.get(msg.get('user_id', user),'?')} "
                      f"{msg.get('side')} {msg.get('volume')}@{msg.get('price')} "
                      f"{msg.get('instrument_id')} -> {'ERR '+str(err) if err else 'ok'}", flush=True)
                if err: await ws.send(json.dumps(err))
                else: await ws.send(json.dumps(EX.user_update(msg.get("user_id", user))))
            elif t == "delete_order":
                EX.books[msg["instrument_id"]].remove(msg.get("user_id", user), msg.get("order_id"))
                await ws.send(json.dumps(EX.user_update(msg.get("user_id", user))))
    except websockets.ConnectionClosed:
        pass
    finally:
        EX.clients.discard(ws)


async def fire_due_hints():
    """Apply scheduled hints: draw realized outcome, move truth, broadcast hint."""
    t = time.time() - EX.start
    fired = [h for h in EX.hint_queue if h[0] <= t]
    for delay, iid, expr in fired:
        EX.hint_queue.remove((delay, iid, expr))
        if iid not in EX.truth:
            continue
        realized = sample_realized(expr)
        p = EX.inst(iid)["parameters"]
        EX.truth[iid] = max(p["lower_bound"], min(p["upper_bound"], EX.truth[iid] + realized))
        EX.overshoot[iid] += OVERSHOOT_FRAC * realized   # crowd overreacts in the move's direction
        info = ev.parse(expr)
        note = {"type": "notification", "message": {
            "instrument_id": iid, "hint": expr,
            "text": f"{iid}: value moves by {expr} (EV {info.mean:+.2f})"}}
        print(f"  [HINT t={t:4.0f}s] {iid} {expr}: EV {info.mean:+.2f}, "
              f"realized {realized:+.2f} -> truth {EX.truth[iid]:.2f}")
        payload = json.dumps(note)
        for ws in list(EX.clients):
            try: await ws.send(payload)
            except Exception: pass


async def noise_loop():
    """Heterogeneous irrational traders. Each agent forms a private, biased,
    noisy belief about value and either rests quotes around it or crosses —
    with random panics and outright mistakes. Emergent price is messy and
    non-deterministic, so our bot must be robust rather than overfit."""
    while True:
        await asyncio.sleep(NOISE_INTERVAL)
        if EX.settled:
            continue
        await fire_due_hints()
        for inst in EX.tradeable:
            iid = inst["id"]; tick = inst["tick_size"]; p = inst["parameters"]
            lo, hi = p["lower_bound"], p["upper_bound"]
            EX.overshoot[iid] *= OVERSHOOT_DECAY
            b = EX.books[iid]
            mid = ((b.best_bid() or EX.truth[iid]) + (b.best_ask() or EX.truth[iid])) / 2
            # churn resting agent orders so the book turns over
            for lst in (b.bids, b.asks):
                lst[:] = [o for o in lst if not o.user.startswith("p") or random.random() > 0.35]
            for a in EX.agents:
                if random.random() > a["activity"]:
                    continue
                name = a["name"]
                # target belief: value anchor + crowd overshoot (signed by personality)
                # + random traders mostly drift off last mid
                anchor = a["value_weight"] * EX.truth[iid] + (1 - a["value_weight"]) * mid
                target = anchor + a["overreact"] * EX.overshoot[iid]
                bel = EX.belief[name][iid]
                bel += a["lag"] * (target - bel) + random.gauss(0, a["noise"] * tick)
                bel = max(lo + tick, min(hi - tick, bel))
                EX.belief[name][iid] = bel
                # panic: all-in one side
                if random.random() < a["panic"]:
                    side = random.choice(["bid", "ask"])
                    px = b.best_ask() if side == "bid" else b.best_bid()
                    if px is not None:
                        EX.insert_order(name, {"instrument_id": iid, "side": side,
                                        "price": px, "volume": random.randint(4, 12)})
                    continue
                if random.random() < a["aggress"]:
                    # cross toward belief; `mistake` flips the side irrationally
                    want = "bid" if bel > mid else "ask"
                    if random.random() < a["mistake"]:
                        want = "ask" if want == "bid" else "bid"
                    px = b.best_ask() if want == "bid" else b.best_bid()
                    if px is not None:
                        EX.insert_order(name, {"instrument_id": iid, "side": want,
                                        "price": px, "volume": random.randint(1, a["size"])})
                else:
                    # rest a two-sided quote around belief
                    sp = tick * a["spread"]
                    bid = round((bel - sp) / tick) * tick
                    ask = round((bel + sp) / tick) * tick
                    if lo <= bid: b.add(Order(name, iid, "bid", bid, random.randint(1, a["size"])))
                    if ask <= hi: b.add(Order(name, iid, "ask", ask, random.randint(1, a["size"])))
            b._sort()


async def broadcast_loop():
    while True:
        await asyncio.sleep(TICK_BROADCAST)
        if SETTLE_AFTER and not EX.settled and time.time() - EX.start > SETTLE_AFTER:
            EX.settle()
            print("\n==== SETTLEMENT ====")
            for iid in EX.truth:
                print(f"  {iid} settled at {EX.inst(iid)['expiration_value']}")
            lb = EX.leaderboard()
            agents = {a["name"] for a in EX.agents}
            for alias, score in lb:
                tag = "  <-- OUR BOT" if (alias not in agents and alias != "seed") else ""
                print(f"  {alias:16} {score:+.2f}{tag}")
            print("RESULT " + json.dumps({"settle": EX.inst(list(EX.truth)[0])["expiration_value"],
                                          "lb": lb, "agents": list(agents)}), flush=True)
            if os.environ.get("DD_BATCH"):
                await asyncio.sleep(0.3)
                os._exit(0)
        if not EX.clients:
            continue
        snap = json.dumps(EX.shared_update())
        dead = []
        for ws in list(EX.clients):
            try:
                await ws.send(snap)
            except Exception:
                dead.append(ws)
        for ws in dead:
            EX.clients.discard(ws)


async def main():
    print(f"[*] Mock Delta Dash exchange on ws://{HOST}:{PORT}/ws")
    print(f"    instruments: {[i['id'] for i in EX.instruments]}  "
          f"tradeable: {[i['id'] for i in EX.tradeable]}")
    print(f"    base values: {({k: round(v,2) for k,v in EX.base_truth.items()})}")
    print(f"    hints scheduled: {len(EX.hint_queue)}, settle after {SETTLE_AFTER}s — limit {POSITION_LIMIT}")
    # Echo back whatever basic.<...> subprotocol the client offers.
    def select_subprotocol(connection, subprotocols):
        return subprotocols[0] if subprotocols else None
    async with websockets.serve(handler, HOST, PORT, select_subprotocol=select_subprotocol):
        await asyncio.gather(noise_loop(), broadcast_loop())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] stopped")
