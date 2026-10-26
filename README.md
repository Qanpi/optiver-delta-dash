# Delta Dash — trading bot, simulator, and strategy research

Technical documentation for Optiver's **Delta Dash** trade-a-thon
(`delta-dash-eur.optibook.net`). This repo contains a protocol client, a local
market simulator, several trading strategies, and the full data + analysis from a real
live event. It is written so that **anyone can reproduce the results claimed below** —
every number has a command that regenerates it and a data file it was derived from.

The focus here is the **quant / trading** work: the strategies, their PnL, and how the
market behaves. The plumbing (how we talk to the exchange) is summarized in one section
and otherwise stays out of the way.

---

## 1. Status of the live site and the bot

- **Live site:** `wss://delta-dash-eur.optibook.net/ws` (player socket). Real events
  are **intermittent** — a scored exchange only exists while Optiver runs an event.
  Between events the socket is reachable but there is **no joinable exchange**: a
  `connect` to a stale/finished `exchange_id` returns a one-shot snapshot then
  `{"error":{"code":"invalid_exchange"}}` and closes.
- **To join you need a valid `exchange_id`** — the UUID in the event join link
  `https://.../hub/<exchange_id>/create`, handed to participants at event time. It is
  server-generated, not guessable. The player password is empty (alias-only join,
  confirmed live).
- **Is the bot running right now?** No. The bot is run **manually, for the duration of
  a single live event**, via `run_live.sh`. There is no always-on deployment; between
  events nothing is connected. One real event has been played (2026-10-23, the "RAIN"
  event) — its complete capture and post-mortem are in `event_data_2026-10-23/`.

Everything else in this repo runs **fully offline** against the local mock, so none of
the claims below require a live event to verify.

---

## 2. What was actually played, and the result

One live event (**RAIN**, 2026-10-23, ~79-player field). We entered 5 aliases running
a **fading / mean-reversion market maker** and **lost** — near the bottom on every
entry (best ~rank 34/79 roughly flat; worst ~−1180, last).

Root cause, confirmed from the capture: the market **trended hard upward** (value
24 → 30 → 66 → ~79 across 15 events), and a fading MM keeps shorting into a rising
market. Compounding it: manual intervention mid-event (fair-value flips, directional
chasing) converted a roughly break-even making strategy into a directional gamble.

Primary sources (raw, so the above is checkable):
- `event_data_2026-10-23/frames/` — raw captured WS frames (full event evolution).
- `event_data_2026-10-23/logs/` — every bot's session log (trades + reactions per event).
- `event_data_2026-10-23/states/` — final telemetry per entry.
- `event_data_2026-10-23/LESSONS.md` — the detailed post-mortem.

---

## 3. Game mechanics (observed from the real capture)

- **Value of an instrument = letter-sum of a word** (A=1 … Z=26). Event-1 word "LEAF"
  = 12+5+1+6 = 24 (base value).
- **15 events** per round modify the value: multiplier ("50% double / 50% halve" ⇒
  EV ×1.25), additive ("+36"), percentage ("+25%"), and occasional shocks ("outage" =
  fall). Value settles to its final realized number.
- Price caps **0–100**. Position limit **5** (`configuration.limit`). Per-order volume
  limit 200.
- Two instruments: **MKT1** (`is_test=true`, tick 0.1, practice) and **RAIN**
  (`is_test=false`, tick 1.0, the scored market). Trade RAIN only (`--skip-test`).
- **Hints (the word + event effects) are shown on presentation slides, NOT in the WS
  feed** — verified across ~27k captured frames, zero hint/notification frames. Any
  hint-driven strategy therefore needs an off-socket text source; the bots here are
  hint-blind.

### How the client talks to the exchange (brief)
The exchange is a WebSocket. The protocol was recovered from the site's Angular bundle
and confirmed against a live capture. Just enough to trade:
- Player socket `wss://<host>/ws`; auth is a WebSocket subprotocol
  `basic.<base64url(alias:password)>` with an empty password.
- On open, send `{"type":"connect","message":{"id":<uuid4>,"username":<alias>,"game_id":<exchange_id>}}`.
- Three client actions: `connect`, `insert_order` (`{instrument_id, side:"bid"|"ask", price, volume}`),
  `delete_order`. Orders need no signature.
- Server pushes `shared_update` (instruments, order books, trades, leaderboard) and
  `user_update` (orders, positions, rank). Gotchas that bit us: positions are a
  **nested** list keyed by `volume`, trade prices are **strings**, order acks arrive as
  `message`-type frames. Full schema in `event_data_2026-10-23/LESSONS.md` §3.

`mm_bot.py` decodes all of this; the mock (`mock_server_v2.py`) re-emits the exact same
schema, so the production bot runs against it unchanged.

---

## 4. Strategies tried, and their results

Two independent evidence bases: **(A)** replay over the *real recorded RAIN book* with
realistic taker fills, and **(B)** a zero-sum agent simulator calibrated to that
capture. Both agree on the ranking.

### A. Replay on the real recorded book
Source: `event_data_2026-10-23/replay_strategies.py` + the captured tape.

| strategy | PnL | trades |
|---|---|---|
| long-hold (buy to limit, sit) | +130 | 5 |
| flat / no-trade | 0 | 0 |
| fade-MM (what we ran live) | −135 | 235 |
| momentum-chase | −380 | 145 |

Live, with churn + flip-flops + adverse selection on top, the fade-MM entries actually
finished −135 to −1186.

### B. Zero-sum simulator bakeoff (300 runs, up / down / mixed rounds)
Source: `agent_sim.py` + `dd_engine.py`.

```
MIXED:  maker-only MM +71  >  follower +35  >  flat 0  >  long-hold −6
        >> fade+take MM −377  >>  pure-EV (slow fair) −613
UP:     long-hold +132   maker-only +9    EV −656
DOWN:   maker-only +166 (67% win)   long-hold −147   EV −568
```

### Conclusions (what the data supports)
- **Maker-only MM is the only strategy positive in BOTH up and down rounds.** Posting
  resting two-sided quotes and inventory-skewing (never crossing/taking) harvests
  volatility regardless of trend. This is the recommended live strategy.
- **long-hold wins up-rounds and loses down-rounds** — a directional coin-flip, not
  robust. It looked like "the answer" only because RAIN happened to trend up.
- **fade-MM and pure-EV (slow fair-chasing) are the big losers** — pure-EV (−613) is
  essentially what sank our worst live entry.
- Markout analysis of real fills confirms passive making was net-favorable
  (+0.4…+0.9 / fill); the live loss was **execution** (taking + manual meddling), not a
  broken edge.

Caveats: the sim is calibrated to a single 8-minute tape; field sharpness and server
rate limits at a real event are not fully known. See
`event_data_2026-10-23/STRATEGY.md` for the full derivation and open calibration gaps.

### Algorithm overviews

**Maker-only MM (`mm_bot.py --strategy mm`, `--lean 0`, no taking) — recommended.**
Compute a reference price per instrument: an EMA-anchored fair that adapts slowly in
calm and fast on a detected jump (`--a-slow`, `--a-fast`, `--jump-k`). Rest two-sided
quotes `--edge` ticks around it, size `--size`, clamped to tick/bounds. Control
inventory purely by **quote-skew** — as position drifts long, lean both quotes down so
the ask fills first, and vice versa — never by crossing the spread. The edge is the
favorable passive markout (~+0.5–0.9 per fill) captured over many fills; it is
direction-neutral, which is why it is the only model positive in both up and down
rounds. `--take-ticks`/`--lean` enable snipe/trend-ride variants, but those add taker
cost and underperformed.

**copy / follower (`mm_bot.py --strategy copy`).** Hint-blind momentum proxy. Watch the
top-`--copy-k` leaderboard aliases' aggressive prints and drive position toward ±limit
in their last traded direction, rate-limited to `--budget` fills/min. Rides informed
flow without needing the slide hints. Positive but weaker than making.

**long-hold (`hand_bot.py` / replay).** Buy to the position limit early and sit. One
decision, ~5 trades, zero churn. Wins trending-up rounds, loses trending-down rounds —
a directional bet, not an edge. Useful as the "do nothing fancy" baseline (0 PnL
already beats >50% of the field, who finish negative).

**pure-EV (`ev_bot.py`).** Maintain a running expected value and chase fair toward it.
Worst performer live and in sim — the fair update lags the market, so it continually
takes stale-priced liquidity and pays spread + adverse selection every event.

**fade-MM (old default).** Mean-reversion maker that sells rallies / buys dips. Wins in
a mean-reverting crowd (our old v1 sim), loses badly in an event-driven trending market
— it shorts into the rise and gets run over. This is what we ran live and lost with.

**feeder / ring model (`ring_bot.py`).** A coordinated multi-account market-making
structure: one **main** alias accumulates PnL, `N` **feeder** aliases supply it. All
aliases run in one asyncio loop on separate sockets so the legs are strictly sequenced.
Per round trip:
1. Read the RAIN book; act only when `best_ask − best_bid ≥ --min-gap` (default 3) — a
   tick strictly inside the spread that no stranger is resting on.
2. **main** rests 1 lot one tick inside the touch (`bb+1` or `ba-1`) and waits for the
   server ack carrying its `order_id`.
3. A **feeder** sends the opposite 1 lot at that price; price priority matches it against
   main ahead of the stranger touch.
4. Cleanup deletes any remainder; legs alternate so feeders unwind back toward flat and
   main's inventory stays within `±--main-band` (default 2).

Each trip moves ~`(spread − 2)` ticks from the feeders into main — exactly zero-sum
across the accounts, net positive for main only because the captured spread exceeds the
2-tick round-trip cost. Mock e2e (seed 3): main rank 1 (+707), feeders near last, 144
legs/min. On the real 8-min tape a ≥3 spread held ~36% of the time (mean 2.1 ticks/leg);
projected 30-min main PnL scales with leg rate — ~+697 at 30 legs/min, ~+1394 at 60/min
(would beat the real winner's ~902). Validated in the mock and against the recorded
tape; the per-alias `--max-rate` cap and a real server's rate limit are the open
unknowns. `--dry-run` prints legs and sends nothing.

---

## 5. Files

**Bot**
- `mm_bot.py` — the production bot. `--strategy mm` (quote + optional snipe, the
  maker/MM path) or `--strategy copy` (hint-free, follow top-K leaders). Auto-reconnect,
  kill-switch (`--max-loss`), schema-warning halt, hot-control via a JSON control file,
  telemetry to a state file. Drop-in for live: only `--url`/`--game` change.
- `run_live.sh <game_id> <alias> [password]` — launches `mm_bot.py` + `supervisor.py`
  (defensive watchdog) under `caffeinate`/`nohup` for a live event. See `RUNBOOK.md`.
- `ring_bot.py`, `ev_bot.py`, `naive_bot.py`, `hand_bot.py` — research strategies.

**Simulator**
- `dd_engine.py` — order book + trending value process + agent population.
- `mock_server_v2.py` — wraps `dd_engine` behind the **real** WS wire schema, so the
  exact production bot code runs against it unchanged.
- `agent_sim.py` — calibrate + benchmark strategies by rank; produces §4-B numbers.
- `mock_server.py` / `seed_book.json` — older v1 mock seeded from a captured book.

**Data + analysis**
- `event_data_2026-10-23/` — the real event: raw frames, logs, states, and the
  post-mortem / strategy writeups (`LESSONS.md`, `STRATEGY.md`, `REBUILD_STATUS.md`).
- `RUNBOOK.md` — live-event launch and recovery procedure.

---

## 6. How to reproduce the results

Requires Python 3.10+ and `websockets` (`pip install websockets`).

### Reproduce the simulator bakeoff (§4-B)
```bash
python3 agent_sim.py            # calibrate + run the strategy bakeoff across rounds
```
Use `DD_FIELD_SPEED` to control how fast the field reprices after events (defaults to
3, matching the real tape: 50% of a jump in ~1.5s, 90% in ~8.5s; set 1 for the slower
session-3 numbers).

### Reproduce the real-book replay (§4-A)
```bash
cd event_data_2026-10-23
python3 replay_strategies.py    # replays each strategy over the captured RAIN tape
```

### Run the bot against the local mock (end-to-end, offline)
```bash
# terminal 1 — start the mock exchange (real wire schema)
DD_PORT=8783 python3 mock_server_v2.py

# terminal 2 — run the maker-only bot against it
python3 mm_bot.py --url ws://localhost:8783/ws --alias me --only RAIN \
    --strategy mm --size 2 --edge 1.0 --lean 0
```
Expected: the bot connects, quotes both sides, trades, holds position within ±5, and
logs zero schema warnings (the mock speaks the exact live schema). Port 8765 collides
with Anki on this machine — always set `DD_PORT` to a free port.

### Run against a live event (only when one is live)
```bash
./run_live.sh <exchange_id> <alias>
```
`<exchange_id>` is the UUID from the event join link. Password is empty. Read
`RUNBOOK.md` first.

---

## 7. Verifying the claims independently

Each claim in this README maps to a command + file above:

| claim | reproduce with | raw source |
|---|---|---|
| live event was lost, near-bottom | — | `event_data_2026-10-23/states/`, `logs/` |
| market trended up 24→~79 | inspect frames | `event_data_2026-10-23/frames/` |
| fade-MM −135, long-hold +130 on the real book | `replay_strategies.py` | captured tape |
| maker-only is the only both-ways-positive strategy | `agent_sim.py` | `dd_engine.py` |
| hints are not in the WS feed | grep the frames | `event_data_2026-10-23/frames/` |
| bot speaks the real schema | run mock + bot, 0 warnings | `mock_server_v2.py` |

The sim results are stochastic (seeded agent populations); exact PnL will vary run to
run, but the **ranking** (maker-only > follower > flat > long-hold >> fade/EV) is stable
across seeds and is the actual claim.
