# Delta Dash — trading bot, simulator, and strategy research

Technical documentation for Optiver's **Delta Dash** trade-a-thon
(`delta-dash-eur.optibook.net`). This repo contains a reverse-engineered protocol
client, a local market simulator, several trading strategies, and the full data +
analysis from a real live event. It is written so that **anyone can reproduce the
results claimed below** — every number has a command that regenerates it and a data
file it was derived from.

> **Scope note.** This is the *trading* side only (bot + sim + strategy). An
> authorized security assessment of the same site lives outside this repo and is not
> documented here.

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

Full protocol schema (frame types, nested positions, string trade prices, auth
subprotocol) is in `event_data_2026-10-23/LESSONS.md` §3.

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
`event_data_2026-10-23/STRATEGY.md` and `delta-dash-sim-rebuild` memory for the full
derivation and open calibration gaps.

> **Collusion / wash-trading (ring).** A multi-account PnL-transfer strategy
> (`ring_bot.py`) was studied in simulation only and is **not run live.** It is included
> for completeness of the research record.

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
