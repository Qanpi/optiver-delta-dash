#!/usr/bin/env python3
"""
dd_engine — zero-sum agent-based Delta Dash market, calibrated to the real RAIN capture
(event_data_2026-10-23). Shared by the offline sim (agent_sim.py) and mock_server.py.

Design (every PnL is a transfer between participants, so the game is zero-sum):
  - Price-time priority limit order book, tick 1, prices bounded [0,100], position limit 5.
    Each 1-lot match prints one trade (real feed: 99.6% of trades are volume 1).
  - Hidden value process: base = word letter-sum, 15 announced EVENTS. Deterministic events
    (add / pct) move fair immediately. Coin events (x2 / x0.5) move public EV by the
    expectation at announce and RESOLVE later, so settlement != final EV.
  - Explicit opponent agents, mix calibrated to real per-player stats:
      fast    : read hint in 1-4s, take toward new EV up to full limit, exit passively.
                (real Colin: bursts of 5 lots right at events)
      slow    : same idea, 6-40s late, noisy EV math, 1-3 lots.
      maker   : quote around a lagging belief; some pull quotes on events, some do not.
      noise   : random small takes, ignore events.
      churner : momentum-chase recent price moves, overtrade (real: activity-PnL corr -0.36).
      idle    : join, never trade (real: ~10 of 77 never printed a trade).
  - Strategies under test plug in as `Participant` subclasses; same API as agents.

Calibration targets (real, final pre-settlement leaderboard, 77 players):
  mean 0, stdev ~305, top [952, 791, 692, 506, 433], worst -1040, ~10% at exactly 0.
  spread median 2 / p90 7, top-of-book size median 3, trade rate ~1.7/s, buy-aggr share ~58%.
"""
from __future__ import annotations
import itertools, math, random
from dataclasses import dataclass, field

TICK = 1
LO, HI = 0, 100
LIMIT = 5
N_EVENTS = 15
DT = 0.5                       # seconds per engine step

WORDS = ["LEAF", "RAIN", "CLOUD", "STORM", "SUN", "WIND", "SNOW", "FROST", "MIST", "HAIL",
         "FOG", "DEW", "BOLT", "WAVE", "TIDE", "OAK", "PINE", "ROSE", "SAND", "ROCK"]


def letter_sum(w):
    return sum(ord(c) - 64 for c in w.upper() if c.isalpha())


def clamp(x, lo=LO, hi=HI):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- value process
@dataclass
class Event:
    t: float                   # announce time (s)
    kind: str                  # add | pct | coin | shock
    text: str
    ev_delta: float = 0.0      # change in public EV at announce
    resolve_t: float | None = None   # coin events: when the outcome becomes known
    outcome_mult: float = 1.0


def make_events(rng, duration, base, drift=None):
    """15 events, roughly evenly spaced with jitter. Returns (events, base).
    drift biases the sign of add/pct events (+1 up-round, -1 down-round, None random)."""
    if drift is None:
        drift = rng.choice([1, -1])
    gap = duration / (N_EVENTS + 1)
    evs = []
    ev_val = float(base)
    for i in range(N_EVENTS):
        t = gap * (i + 1) + rng.uniform(-0.25, 0.25) * gap
        r = rng.random()
        sgn = drift if rng.random() < 0.7 else -drift
        if r < 0.20 and 8 < ev_val < 60:
            # coin: 50% double / 50% halve. EV x1.25 at announce, resolve 30-120s later.
            e = Event(t, "coin", "50% chance to double, 50% to halve")
            e.outcome_mult = rng.choice([2.0, 0.5])
            e.resolve_t = t + rng.uniform(30, 120)
            new = clamp(ev_val * 1.25)
        elif r < 0.55:
            n = rng.choice([3, 5, 6, 8, 10, 12, 15, 20, 25, 36])
            e = Event(t, "add", f"{'up' if sgn > 0 else 'down'} by {n} points")
            new = clamp(ev_val + sgn * n)
        elif r < 0.85:
            p = rng.choice([5, 10, 15, 20, 25, 30])
            e = Event(t, "pct", f"{'+' if sgn > 0 else '-'}{p}%")
            new = clamp(ev_val * (1 + sgn * p / 100))
        else:
            n = rng.choice([15, 20, 25, 30])
            s = rng.choice([1, -1])
            e = Event(t, "shock", f"shock {'+' if s > 0 else '-'}{n}")
            new = clamp(ev_val + s * n)
        if e.kind != "coin" and not (5 <= new <= 95):
            # reflect off the caps: the real path ranged 26-84 and never pinned at 0/100.
            # Flip the move's direction (and its text) rather than let EV stick to a bound.
            new = clamp(2 * ev_val - new)
            e.text = (e.text.replace("up", "\0").replace("down", "up").replace("\0", "down")
                      .replace("+", "\0").replace("-", "+").replace("\0", "-"))
        e.ev_delta = new - ev_val
        ev_val = new
        evs.append(e)
    return evs


class ValueProcess:
    """Tracks public EV (what a perfect, instant hint-reader knows) and the realized value."""

    def __init__(self, rng, duration, word=None, drift=None):
        self.word = word or rng.choice(WORDS)
        self.base = clamp(letter_sum(self.word))
        self.events = make_events(rng, duration, self.base, drift)
        self.ev = float(self.base)          # public expected settlement
        self.realized_mult = 1.0
        self.pending = []                   # coin events awaiting resolution
        self.fired = 0

    def step(self, t):
        """Advance to time t. Returns list of (kind, Event) fired this step."""
        out = []
        while self.fired < len(self.events) and self.events[self.fired].t <= t:
            e = self.events[self.fired]
            self.fired += 1
            self.ev = clamp(self.ev + e.ev_delta)
            if e.kind == "coin":
                self.pending.append(e)
            out.append(("announce", e))
        for e in list(self.pending):
            if e.resolve_t <= t:
                self.pending.remove(e)
                # replace the x1.25 expectation with the realized multiplier
                self.ev = clamp(self.ev / 1.25 * e.outcome_mult)
                out.append(("resolve", e))
        return out

    def ev_naive(self):
        """EV as seen by a player who prices '50% double / 50% halve' at x1.0 (the common
        geometric-mean mistake) instead of x1.25, until the coin resolves."""
        return clamp(self.ev / (1.25 ** len(self.pending)))

    def settle_value(self):
        v = self.ev
        for e in self.pending:              # unresolved coins resolve at settlement
            v = v / 1.25 * e.outcome_mult
        return round(clamp(v))


# ---------------------------------------------------------------- order book
_oid = itertools.count(1)
_tid = itertools.count(1)


class Order:
    __slots__ = ("id", "owner", "side", "price", "vol", "seq")

    def __init__(self, owner, side, price, vol):
        self.id = str(next(_oid)); self.owner = owner; self.side = side
        self.price = int(price); self.vol = int(vol); self.seq = next(_oid)


class Book:
    """Price-time priority. Bids sorted price desc then seq asc, asks price asc then seq asc."""

    def __init__(self):
        self.bids: list[Order] = []
        self.asks: list[Order] = []

    def best_bid(self):
        return self.bids[0].price if self.bids else None

    def best_ask(self):
        return self.asks[0].price if self.asks else None

    def mid(self, default=None):
        bb, ba = self.best_bid(), self.best_ask()
        if bb is not None and ba is not None:
            return (bb + ba) / 2
        return bb if bb is not None else (ba if ba is not None else default)

    def _insert_resting(self, o):
        lst = self.bids if o.side == "bid" else self.asks
        key = (lambda x: (-x.price, x.seq)) if o.side == "bid" else (lambda x: (x.price, x.seq))
        # binary insert
        k = key(o); lo, hi = 0, len(lst)
        while lo < hi:
            m = (lo + hi) // 2
            if key(lst[m]) < k: lo = m + 1
            else: hi = m
        lst.insert(lo, o)

    def cancel(self, owner, oid):
        for lst in (self.bids, self.asks):
            for i, o in enumerate(lst):
                if o.id == oid and o.owner is owner:
                    lst.pop(i); return True
        return False

    def cancel_all(self, owner, side=None):
        if side in (None, "bid"):
            self.bids = [o for o in self.bids if o.owner is not owner]
        if side in (None, "ask"):
            self.asks = [o for o in self.asks if o.owner is not owner]

    def orders_of(self, owner):
        return [o for o in self.bids + self.asks if o.owner is owner]

    def level_size(self, side, price):
        lst = self.bids if side == "bid" else self.asks
        return sum(o.vol for o in lst if o.price == price)

    def aggregate(self, depth=None):
        def agg(lst, key):
            out = {}
            for o in lst:
                out[o.price] = out.get(o.price, 0) + o.vol
            items = sorted(out.items(), key=lambda kv: -kv[0] if key == "bid" else kv[0])
            if depth: items = items[:depth]
            return [{key: v, "price": float(p)} for p, v in items]
        return agg(self.bids, "bid"), agg(self.asks, "ask")


# ---------------------------------------------------------------- participants
class Participant:
    """Base class. Agents and strategies-under-test share it."""
    kind = "base"

    def __init__(self, name, rng):
        self.name = name; self.rng = rng
        self.pos = 0; self.cash = 0.0; self.n_trades = 0; self.n_maker = 0

    def pnl(self, mark):
        return self.cash + self.pos * mark

    def room(self, side):
        return (LIMIT - self.pos) if side == "bid" else (LIMIT + self.pos)

    def on_step(self, mkt):
        """Called once per engine step. Use mkt.insert / mkt.cancel / mkt.cancel_all."""

    def on_event(self, mkt, kind, ev):
        """Called when an event is announced/resolved. Subclasses schedule their reaction."""

    def on_fill(self, mkt, side, price, maker):
        pass


class Market:
    def __init__(self, seed=None, duration=1800.0, drift=None, word=None):
        self.rng = random.Random(seed)
        self.duration = duration
        self.t = 0.0
        self.book = Book()
        self.value = ValueProcess(self.rng, duration, word=word, drift=drift)
        self.parts: list[Participant] = []
        self.trades: list[dict] = []      # full history
        self.last_price = float(self.value.base)
        self.event_log: list[tuple] = []
        self.settled = None

    # ---- participant API
    def add(self, p):
        self.parts.append(p); return p

    def insert(self, owner, side, price, vol=1):
        """Place a limit order. Crosses immediately where marketable. Volume clipped to the
        owner's position room (the real server rejects/clips beyond limit). Returns order or None."""
        price = int(round(clamp(price)))
        vol = min(int(vol), owner.room(side))
        if vol <= 0:
            return None
        o = Order(owner, side, price, vol)
        opp = self.book.asks if side == "bid" else self.book.bids
        while o.vol > 0 and opp:
            m = opp[0]
            if (side == "bid" and m.price > price) or (side == "ask" and m.price < price):
                break
            if m.owner is owner:            # no self-trade: cancel own resting order
                opp.pop(0); continue
            mroom = m.owner.room(m.side)
            if mroom <= 0:
                opp.pop(0); continue        # maker at limit -> order dropped
            # one print per lot (matches real feed)
            m.vol -= 1; o.vol -= 1
            self._fill(buyer=owner if side == "bid" else m.owner,
                       seller=m.owner if side == "bid" else owner,
                       price=m.price, aggressor=side, maker=m.owner)
            if m.vol <= 0:
                opp.pop(0)
            if owner.room(side) <= 0:
                o.vol = 0
        if o.vol > 0:
            self.book._insert_resting(o)
            return o
        return None

    def cancel(self, owner, oid):
        return self.book.cancel(owner, oid)

    def cancel_all(self, owner, side=None):
        self.book.cancel_all(owner, side)

    def _fill(self, buyer, seller, price, aggressor, maker):
        buyer.pos += 1; buyer.cash -= price; buyer.n_trades += 1
        seller.pos -= 1; seller.cash += price; seller.n_trades += 1
        maker.n_maker += 1
        tr = {"trade_id": next(_tid), "t": self.t, "price": price, "volume": 1,
              "aggressor_side": aggressor, "buyer": buyer.name, "seller": seller.name}
        self.trades.append(tr)
        self.last_price = price
        buyer.on_fill(self, "bid", price, buyer is maker)
        seller.on_fill(self, "ask", price, seller is maker)

    # ---- views
    def mid(self):
        return self.book.mid(default=self.last_price)

    def mark(self):
        return self.settled if self.settled is not None else self.mid()

    def recent_prices(self, seconds):
        cut = self.t - seconds
        return [tr["price"] for tr in reversed(self.trades) if tr["t"] >= cut][::-1]

    # ---- engine
    def step(self):
        self.t += DT
        for kind, e in self.value.step(self.t):
            self.event_log.append((self.t, kind, e.text, round(self.value.ev, 2)))
            for p in self.parts:
                p.on_event(self, kind, e)
        order = list(self.parts)
        self.rng.shuffle(order)               # random priority each step (no structural edge)
        for p in order:
            p.on_step(self)

    def run(self):
        while self.t < self.duration:
            self.step()
        self.settle()
        return self

    def settle(self):
        self.settled = self.value.settle_value()
        self.book = Book()

    def leaderboard(self):
        m = self.mark()
        rows = [(p.name, round(p.pnl(m), 1), p) for p in self.parts]
        rows.sort(key=lambda r: -r[1])
        return rows


# ---------------------------------------------------------------- calibrated agents
class Agent(Participant):
    """Shared helpers for opponent agents."""

    def __init__(self, name, rng, **kw):
        super().__init__(name, rng)
        self.__dict__.update(kw)
        self.belief = None
        self.ev_seen = None         # last public EV this agent has read (lagged, noisy)
        self.pending = []           # (fire_time, new_belief)

    def init_belief(self, mkt):
        if self.belief is None:
            self.belief = mkt.value.base + self.rng.gauss(0, self.err)

    def schedule_update(self, mkt, delay):
        """Re-read public EV after `delay` seconds, with personal arithmetic error."""
        ev = mkt.value.ev_naive() if getattr(self, "coin_naive", False) else mkt.value.ev
        target = ev + self.rng.gauss(0, self.err)
        self.pending.append((mkt.t + delay, clamp(target)))

    def apply_pending(self, mkt):
        fired = False
        for item in list(self.pending):
            if item[0] <= mkt.t:
                self.pending.remove(item)
                self.belief = self.ev_seen = item[1]; fired = True
        return fired

    def take(self, mkt, side, n=1, worst=None):
        """Cross the spread for up to n lots, not paying through `worst`."""
        px = mkt.book.best_ask() if side == "bid" else mkt.book.best_bid()
        if px is None:
            return
        if worst is not None:
            if side == "bid": px = min(int(worst), HI)
            else: px = max(int(worst), LO)
        mkt.insert(self, side, px, n)


class FastInformed(Agent):
    """Reads hints quickly; trades full-size toward new EV, then unwinds passively near EV.
    real analog: Colin, HenryWang (bursts of 5, then passive exits)."""
    kind = "fast"

    maxlot = LIMIT        # how many lots this agent will carry (real informed rarely max out)

    def on_event(self, mkt, kind, e):
        self.init_belief(mkt)
        # only chase MEANINGFUL moves; small jitters aren't worth the spread (real informed
        # don't fire on every event, which is why even the winner only made a few hundred).
        if abs(getattr(e, "ev_delta", 0)) < self.minmove and kind != "resolve":
            return
        self.schedule_update(mkt, self.rng.uniform(*self.delay))

    def on_step(self, mkt):
        self.init_belief(mkt)
        fresh = self.apply_pending(mkt)
        b = self.belief; bb, ba = mkt.book.best_bid(), mkt.book.best_ask()
        cap = self.maxlot
        if fresh:
            mkt.cancel_all(self)
            if ba is not None and ba < b - self.edge and self.pos < cap:
                self.take(mkt, "bid", cap - self.pos, worst=b - self.edge)
            elif bb is not None and bb > b + self.edge and self.pos > -cap:
                self.take(mkt, "ask", cap + self.pos, worst=b + self.edge)
        if self.rng.random() < self.requote:
            # passive layer: exit inventory near belief, also quote around it
            mkt.cancel_all(self)
            w = self.width
            skew = self.pos * 0.5
            if self.pos < LIMIT:
                mkt.insert(self, "bid", round(b - w - skew), self.rng.randint(1, 2))
            if self.pos > -LIMIT:
                mkt.insert(self, "ask", round(b + w - skew), self.rng.randint(1, 2))


class SlowInformed(FastInformed):
    """Same logic, slow and sloppy. Most of the field."""
    kind = "slow"


class Maker(Agent):
    """Quotes around a belief that tracks mid plus (with lag) public EV. `pull` makers
    cancel AND stand aside for a cooldown on events (skilled); others leave stale quotes
    to be picked off (the adverse-selection donors). `ev_aware` makers fold the announced
    EV move into belief fast, so they are not permanently stale in a trend."""
    kind = "maker"

    def __init__(self, name, rng, **kw):
        super().__init__(name, rng, **kw)
        self.cool = 0.0

    def on_event(self, mkt, kind, e):
        self.init_belief(mkt)
        if self.pull:
            mkt.cancel_all(self)
            self.cool = mkt.t + self.standaside       # skilled: wait out the dislocation
        self.schedule_update(mkt, self.rng.uniform(*self.delay))

    def on_step(self, mkt):
        self.init_belief(mkt)
        self.apply_pending(mkt)
        m = mkt.mid()
        # belief tracks mid, but ev_aware makers also pull toward the EV they last READ
        # (lagged + noisy, never the live true EV) so they are less stale in a trend
        seen = self.ev_seen if self.ev_seen is not None else m
        target = m if not self.ev_aware else 0.5 * m + 0.5 * seen
        self.belief += self.anchor * (target - self.belief)
        if mkt.t < self.cool or self.rng.random() >= self.requote:
            return
        mkt.cancel_all(self)
        skew = self.pos * self.skew
        sz = self.rng.randint(1, self.size)
        # widen while inventory is heavy or right after cooldown
        w = self.width + (1 if abs(self.pos) >= 3 else 0)
        if self.pos < LIMIT:
            mkt.insert(self, "bid", math.floor(self.belief - w - skew), sz)
        if self.pos > -LIMIT:
            mkt.insert(self, "ask", math.ceil(self.belief + w - skew), sz)


class Noise(Agent):
    kind = "noise"

    def on_step(self, mkt):
        if self.rng.random() < self.rate:
            side = self.rng.choice(["bid", "ask"])
            if self.rng.random() < self.bias_up:
                side = "bid"
            self.take(mkt, side, 1)


class Churner(Agent):
    """Chases recent moves, flips often. Real analogs: pig8, aleksei (us)."""
    kind = "churner"

    def on_step(self, mkt):
        if self.rng.random() > self.rate:
            return
        px = mkt.recent_prices(self.look)
        if len(px) < 3:
            side = self.rng.choice(["bid", "ask"])
        else:
            mv = px[-1] - px[0]
            side = "bid" if mv > 0 else "ask" if mv < 0 else self.rng.choice(["bid", "ask"])
        self.take(mkt, side, self.rng.randint(1, self.size))


class Idle(Agent):
    kind = "idle"


# Population mix for 77 players (76 opponents + 1 slot for the strategy under test).
# Ratios + params tuned by agent_sim.calibrate() against the real leaderboard.
#   - few SKILLED winners (fast + skilled makers) extract from many donors
#   - most of the field is slow/noise ~ flat-to-slightly-losing
#   - a couple of whale churners form the deep losing tail (our real aleksei was one)
# NATURAL field (default): no heavy feeders. Reproduces the un-inflated distribution a
# real field would show WITHOUT our 5 bots funding the tails ~ top ≈ +400, worst ≈ -400.
POP = dict(fast=2, slow=23, maker=14, noise=18, churner=8, idle=12)

# The real 2026-10-23 event was INFLATED by our own feeder bots (aleksei alone -1040,
# funding winner 952). Pass feeders=N to make_population to add N whale over-traders and
# reproduce that fatter-tailed leaderboard (stdev ~305). Default 0 = natural field.
FEEDERS = int(__import__("os").environ.get("DD_FEEDERS", "0"))
SPEED = float(__import__("os").environ.get("DD_FIELD_SPEED", "3.0"))   # session 4: matches real repricing


def make_population(mkt, pop=None, rng=None, feeders=None, naive_coin=0.0, speed=None):
    """naive_coin: share of informed agents and makers that misprice coin events as x1.0
    (default 0 keeps the calibrated field unchanged).
    speed: divides the reaction delays of fast, slow and maker agents (default DD_FIELD_SPEED=3). 1.0 = the session-3
    calibration. The real tape reprices ~5x faster than that field (open question 2)."""
    pop = pop or POP
    sp = SPEED if speed is None else speed
    feeders = FEEDERS if feeders is None else feeders
    r = rng or mkt.rng
    agents = []
    i = 0

    def nm(k):
        nonlocal i
        i += 1
        return f"{k}{i:02d}"
    # fast informed: heterogeneous skill so scores SPREAD (one clear winner, not a tie).
    for j in range(pop.get("fast", 0)):
        skill = (j + 1) / max(1, pop.get("fast", 1))   # 0..1 across the cohort
        # tuned down (session 3): a 1-2s, near-perfect reader cleared ~900 in the natural
        # field; real un-fed top is ~+400. Slower read, noisier math, max 2 lots, rare quoting.
        a = FastInformed(nm("fast"), r, delay=(2.0 / sp, (4.0 + 4 * (1 - skill)) / sp),
                         err=r.uniform(3.0, 6.0),
                         edge=2 if skill > 0.6 else r.choice([2, 3, 4]),
                         requote=r.uniform(0.03, 0.08), width=r.choice([1, 2]),
                         minmove=r.choice([4, 5, 6]))
        a.maxlot = 2
        agents.append(a)
    for _ in range(pop.get("slow", 0)):
        a = SlowInformed(nm("slow"), r, delay=(8.0 / sp, 30.0 / sp), err=r.uniform(1.0, 5.0),
                         edge=r.choice([6, 8, 10]), requote=r.uniform(0.01, 0.05),
                         width=r.choice([2, 3, 4, 6]), minmove=r.choice([6, 8, 10]))
        a.maxlot = r.choice([1, 2])
        agents.append(a)
    # makers: ~60% skilled (pull + stand aside + ev_aware) lose little / sometimes win;
    # ~40% naive stale donors that get picked off (the adverse-selection source).
    for _ in range(pop.get("maker", 0)):
        skilled = r.random() < 0.6
        agents.append(Maker(nm("mm"), r, delay=(1.5 / sp, 10.0 / sp), err=r.uniform(0.5, 4.0),
                            pull=skilled, standaside=r.uniform(4.0, 9.0) if skilled else 0.0,
                            ev_aware=skilled, anchor=r.uniform(0.2, 0.5),
                            requote=r.uniform(0.08, 0.3),
                            width=r.choice([2, 2, 3]) if skilled else 2,
                            skew=r.uniform(0.3, 0.9) if skilled else r.uniform(0.0, 0.3),
                            size=r.choice([1, 2, 3])))
    for _ in range(pop.get("noise", 0)):
        agents.append(Noise(nm("noise"), r, err=0, rate=r.uniform(0.001, 0.01),
                            bias_up=r.uniform(0.0, 0.15)))
    # churners: natural field = moderate over-traders (small donors).
    for _ in range(pop.get("churner", 0)):
        agents.append(Churner(nm("churn"), r, err=0, rate=r.uniform(0.01, 0.035),
                              look=r.choice([5, 10, 20]), size=r.choice([1, 2])))
    # OPTIONAL feeders: whale over-traders = our own bots at the real event. They bleed
    # hard and inflate the winners' scores. Off by default (natural field).
    for _ in range(feeders):
        agents.append(Churner(nm("feeder"), r, err=0, rate=r.uniform(0.18, 0.30),
                              look=r.choice([5, 10, 20]), size=3))
    for _ in range(pop.get("idle", 0)):
        agents.append(Idle(nm("idle"), r, err=0))
    for a in agents:
        if naive_coin and a.kind in ("fast", "slow", "maker") and r.random() < naive_coin:
            a.coin_naive = True
        mkt.add(a)
    return agents
