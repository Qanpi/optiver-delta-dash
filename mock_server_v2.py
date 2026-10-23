#!/usr/bin/env python3
"""
mock_server_v2 — local Delta Dash exchange driven by the zero-sum agent market (dd_engine),
speaking the REAL wire schema captured live on 2026-10-23 (see event_data_2026-10-23/LESSONS.md §3).

Replaces mock_server.py (old schema + mean-reverting crowd). Connected bots trade against the
calibrated dd_engine population in the same RAIN book, so end-to-end tests exercise the live
bot's parsing, order flow and strategy at once.

Wire schema (real):
  - auth: subprotocol basic.<b64url(alias:pw)>, empty pw OK
  - client -> {"type":"connect","message":{id,username,game_id}}
  - client -> {"type":"insert_order","message":{user_id,instrument_id,side,price,volume}}
  - client -> {"type":"delete_order","message":{user_id,instrument_id,order_id}}
  - shared_update {instruments, order_books[{instrument_id,bids[{bid,price}],asks[{ask,price}]}],
                   trades[{instrument_id,price:"54.00",volume,aggressor_side,buyer,seller,trade_id}],
                   leaderboard[[alias,pnl]]}
  - user_update {configuration{limit,leaderboard_visible}, orders[{id,user,user_id,instrument_id,
                 price,volume,side}], positions [[{user_id,instrument_id,cash,volume}]],
                 leaderboard_position}
  - order ack {type:"message", message:{success, description, order_id}}
  - error {type:"error", error:{code}}  (invalid_exchange, alias_already_exists)

The real feed carried NO event/hint frames (none in 27k captured frames) — hints reached players
off-socket. So by default the mock sends none either. For hint-parser work:
  DD_HINT_FILE=path   append each announced event as a JSON line (an off-socket text source)
  DD_HINTS=1          also broadcast {type:"notification"} frames (NOT real; testing only)

Env: DD_PORT (8765), DD_SPEED (1; >1 compresses time), DD_DURATION (1800 sim-seconds),
     DD_SEED, DD_DRIFT (1|-1, default random), DD_GAME (exchange id to require; unset = any),
     DD_FEEDERS (dd_engine whale churners, default 0), DD_BATCH (exit after settlement).

Run:  python3 mock_server_v2.py          # ws://localhost:8765/ws
      python3 mm_bot.py --url ws://localhost:8765/ws --alias me --only RAIN
"""
from __future__ import annotations
import asyncio, base64, json, os, time
import websockets
import dd_engine as E

HOST = "localhost"
PORT = int(os.environ.get("DD_PORT", "8765"))
SPEED = float(os.environ.get("DD_SPEED", "1"))
DURATION = float(os.environ.get("DD_DURATION", "1800"))
SEED = int(os.environ["DD_SEED"]) if os.environ.get("DD_SEED") else None
DRIFT = int(os.environ["DD_DRIFT"]) if os.environ.get("DD_DRIFT") else None
GAME = os.environ.get("DD_GAME") or None
HINT_FILE = os.environ.get("DD_HINT_FILE") or None
HINT_FRAMES = bool(os.environ.get("DD_HINTS"))
BATCH = bool(os.environ.get("DD_BATCH"))
IID = "RAIN"


class Remote(E.Participant):
    """A connected websocket client. Trades only through incoming insert/delete messages."""
    kind = "remote"

    def __init__(self, name, uid, rng):
        super().__init__(name, rng)
        self.uid = uid


class Exchange:
    def __init__(self):
        self.mkt = E.Market(seed=SEED, duration=DURATION, drift=DRIFT)
        E.make_population(self.mkt)
        self.remotes: dict[str, Remote] = {}          # uid -> participant
        self.alias_uid: dict[str, str] = {}           # alias -> uid that registered it
        self.clients: dict = {}                       # ws -> uid
        self.sent_trades = 0
        self.expired = False

    # ---- instruments
    def instruments(self):
        rain = {"id": IID, "name": IID, "tick_size": 1.0, "paused": False,
                "expired": self.expired,
                "expiration_value": float(self.mkt.settled) if self.expired else None,
                "is_test": False, "parameters": {"lower_bound": E.LO, "upper_bound": E.HI}}
        # MKT1 exists on the real exchange as the practice instrument; the mock keeps it
        # visible but empty so bots must handle (and skip) it as they would live.
        mkt1 = {"id": "MKT1", "name": "MKT1", "tick_size": 0.1, "paused": False, "expired": False,
                "expiration_value": None, "is_test": True,
                "parameters": {"lower_bound": 0, "upper_bound": 100}}
        return [mkt1, rain]

    # ---- frames
    def leaderboard(self):
        m = self.mkt.mark()
        return [[p.name, round(p.pnl(m), 2)] for p in
                sorted(self.mkt.parts, key=lambda p: -p.pnl(m))]

    def trade_json(self, tr):
        return {"instrument_id": IID, "price": f"{tr['price']:.2f}", "volume": tr["volume"],
                "aggressor_side": tr["aggressor_side"], "buyer": tr["buyer"],
                "seller": tr["seller"], "trade_id": tr["trade_id"]}

    def shared_update(self, all_trades=False):
        bids, asks = self.mkt.book.aggregate()
        trades = self.mkt.trades if all_trades else self.mkt.trades[self.sent_trades:]
        return {"type": "shared_update", "instruments": self.instruments(),
                "order_books": [{"instrument_id": "MKT1", "bids": [], "asks": []},
                                {"instrument_id": IID, "bids": bids, "asks": asks}],
                "trades": [self.trade_json(t) for t in trades],
                "leaderboard": self.leaderboard()}

    def user_update(self, uid):
        p = self.remotes[uid]
        lb = self.leaderboard()
        rank = next((i + 1 for i, r in enumerate(lb) if r[0] == p.name), 0)
        orders = [{"id": int(o.id), "user": p.name, "user_id": uid, "instrument_id": IID,
                   "price": float(o.price), "volume": o.vol, "side": o.side}
                  for o in self.mkt.book.orders_of(p)]
        return {"type": "user_update",
                "configuration": {"limit": E.LIMIT, "leaderboard_visible": True},
                "orders": orders,
                "positions": [[{"user_id": uid, "instrument_id": "MKT1", "cash": 0.0, "volume": 0},
                               {"user_id": uid, "instrument_id": IID, "cash": round(p.cash, 2),
                                "volume": p.pos}]],
                "leaderboard_position": rank}

    # ---- actions
    def connect(self, uid, alias):
        owner = self.alias_uid.get(alias)
        if owner is not None and owner != uid:
            return {"type": "error", "error": {"code": "alias_already_exists"}}
        if uid not in self.remotes:
            self.alias_uid[alias] = uid
            self.remotes[uid] = self.mkt.add(Remote(alias, uid, self.mkt.rng))
        return None

    def insert_order(self, uid, msg):
        p = self.remotes.get(uid)
        if p is None:
            return ack(False, "Unknown user")
        if msg.get("instrument_id") != IID:
            return ack(False, "Instrument not tradeable")
        if self.expired:
            return ack(False, "Instrument expired")
        side = msg.get("side")
        try:
            price = float(msg.get("price")); vol = int(msg.get("volume"))
        except (TypeError, ValueError):
            return ack(False, "Invalid order")
        if side not in ("bid", "ask") or vol <= 0 or vol > 200:
            return ack(False, "Invalid order")
        if not (E.LO <= price <= E.HI) or abs(price - round(price)) > 1e-9:
            return ack(False, f"Invalid price {price}")
        if p.room(side) <= 0:
            return ack(False, "Position limit reached")
        o = self.mkt.insert(p, side, round(price), vol)
        return ack(True, f"Order inserted @ {price}", int(o.id) if o else None)

    def delete_order(self, uid, msg):
        p = self.remotes.get(uid)
        if p is None:
            return ack(False, "Unknown user")
        ok = self.mkt.cancel(p, str(msg.get("order_id")))
        return ack(ok, "Order deleted" if ok else "Order not found", msg.get("order_id"))


def ack(success, description, order_id=None):
    return {"type": "message",
            "message": {"success": success, "description": description, "order_id": order_id}}


EX = Exchange()


def alias_from_subprotocol(sub):
    if not sub or not sub.startswith("basic."):
        return None
    raw = sub[len("basic."):]
    try:
        dec = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode()
    except Exception:
        return None
    return dec.split(":", 1)[0]


async def send(ws, obj):
    try:
        await ws.send(json.dumps(obj))
    except websockets.ConnectionClosed:
        EX.clients.pop(ws, None)


async def handler(ws):
    sub_alias = alias_from_subprotocol(ws.subprotocol)
    uid = None
    try:
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            t, msg = m.get("type"), m.get("message") or {}
            if t == "connect":
                if GAME and msg.get("game_id") != GAME:
                    await send(ws, {"type": "error", "error": {"code": "invalid_exchange"}})
                    await ws.close(); return
                alias = msg.get("username") or sub_alias or "anon"
                err = EX.connect(msg.get("id"), alias)
                if err:
                    await send(ws, err); await ws.close(); return
                uid = msg.get("id")
                EX.clients[ws] = uid
                print(f"  [JOIN] {alias} ({uid[:8]})", flush=True)
                snap = EX.shared_update(all_trades=True); snap.pop("type")   # first frame: untyped
                await send(ws, snap)
                await send(ws, EX.user_update(uid))
            elif uid is None:
                continue
            elif t == "insert_order":
                await send(ws, EX.insert_order(uid, msg))
                await send(ws, EX.user_update(uid))
            elif t == "delete_order":
                await send(ws, EX.delete_order(uid, msg))
                await send(ws, EX.user_update(uid))
    except websockets.ConnectionClosed:
        pass
    finally:
        EX.clients.pop(ws, None)


def publish_events(fired):
    """Server log + optional off-socket hint file / notification frames."""
    notes = []
    for t, kind, text, ev in fired:
        line = {"t": round(t, 1), "kind": kind, "text": text, "instrument_id": IID}
        print(f"  [EVENT t={t:6.0f}s] {kind:8} {text:40} public EV -> {ev}", flush=True)
        if HINT_FILE:
            with open(HINT_FILE, "a") as f:
                f.write(json.dumps({"ts": time.time(), **line}) + "\n")
        if HINT_FRAMES:
            notes.append({"type": "notification", "message": line})
    return notes


async def engine_loop():
    mkt = EX.mkt
    n_log = 0
    print(f"[*] word {mkt.value.word} base {mkt.value.base}, {len(mkt.value.events)} events, "
          f"{len(mkt.parts)} agents, duration {DURATION:.0f}s at x{SPEED}", flush=True)
    while mkt.t < mkt.duration:
        await asyncio.sleep(E.DT / SPEED)
        mkt.step()
        notes = publish_events(mkt.event_log[n_log:]); n_log = len(mkt.event_log)
        frame = EX.shared_update(); EX.sent_trades = len(mkt.trades)
        for ws, uid in list(EX.clients.items()):
            for n in notes:
                await send(ws, n)
            await send(ws, frame)
            await send(ws, EX.user_update(uid))
    mkt.settle(); EX.expired = True
    frame = EX.shared_update()
    for ws, uid in list(EX.clients.items()):
        await send(ws, frame); await send(ws, EX.user_update(uid))
    lb = EX.leaderboard()
    print(f"\n==== SETTLEMENT {IID} = {mkt.settled} ====")
    for i, (alias, pnl) in enumerate(lb):
        tag = "  <-- REMOTE" if alias in EX.alias_uid else ""
        if i < 10 or tag:
            print(f"  {i + 1:3} {alias:16} {pnl:+9.2f}{tag}")
    remotes = {a: next(i + 1 for i, r in enumerate(lb) if r[0] == a) for a in EX.alias_uid}
    print("RESULT " + json.dumps({"settle": mkt.settled, "n": len(lb), "remote_rank": remotes,
                                  "remote_pnl": {a: r[1] for a, r in zip(remotes, [
                                      next(r for r in lb if r[0] == a) for a in remotes])}}),
          flush=True)
    if BATCH:
        await asyncio.sleep(0.3)
        os._exit(0)
    await asyncio.Future()        # keep serving the expired state


async def main():
    print(f"[*] mock_server_v2 on ws://{HOST}:{PORT}/ws  (game_id {'any' if not GAME else GAME})")

    def select_subprotocol(connection, subprotocols):
        return subprotocols[0] if subprotocols else None
    async with websockets.serve(handler, HOST, PORT, select_subprotocol=select_subprotocol,
                                max_size=None):
        await engine_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] stopped")
