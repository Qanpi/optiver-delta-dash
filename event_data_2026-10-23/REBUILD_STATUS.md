# Delta Dash — sim rebuild status & handoff (2026-10-23, sessions 2-3)

Picks up after the live event loss. Goal: a zero-sum, agent-based simulator calibrated to
the REAL captured RAIN market, so strategies can be benched by finishing RANK (not raw PnL),
and the real bot can be tested end-to-end against a realistic book.


## SESSION 3 UPDATE (read this first — supersedes some session-2 findings below)

### Engine changes (`dd_engine.py`)
- **Leak fixed:** skilled (`ev_aware`) makers were reading the LIVE true EV every step. They now
  pull toward `ev_seen` (the lagged, noisy EV they last read via their scheduled update).
- **Fast cohort tuned down:** delay 2-8s, err 3-6, maxlot 2, rare passive quoting. Slow cohort:
  edge 6-10, delay 8-30s, maxlot 1-2. Makers react in 1.5-10s, anchor 0.2-0.5. Noise/churn rates halved.
- **Cap reflection:** add/pct/shock moves that would leave [5,95] are mirrored (text flipped too).
  Before, EV often pinned at 0 or 100 for the rest of the round; now median path range 24-85
  (real 26-84), 0/200 runs pinned.

### Calibration now (natural field, 30 runs)
```
stdev 215 | top5 [712,596,501,440,399] | worst -481 | flat 17% | profitable 36% | zero-sum ✓
microstructure: 1.08 trades/s (real 1.7 incl. our ~5 bots), spread med 2 / p90 4 (real 2/7), ToB med 3 (real 3)
```
Top still ~1.6x the user's ≈+400 target. Diagnosis: ~100% of the fast agents' PnL is TAKER edge vs
EV (stale quotes right after events; maker edge ≈ 0), and it scales with total path movement. Further
tuning had diminishing returns — the remaining lever is event SIZE, which we have no clean data for
(missing first 14 min). Left as is; rankings are stable across every variant tried.

### Bench now — MAKER-ONLY FINDING REVERSED
```
strategy        mean PnL  rank/77  win%
flat                 0     42      0%
maker-only        -129     58      8%    (was +93 / rank 23 in session 2)
follow-maker       -28     41     35%
instant-EV        +834      1    100%
old-MM (fade)     -934     78      0%
```
- maker-only's +93 was a calibration artifact (over-active noise/churn flow + makers peeking at
  live EV). It loses in every variant tried this session, incl. with `feeders=5` (-82, rank 63).
  ⇒ **passive making without a fast fair is a donor strategy here.** Maker-only mode in mm_bot is
  DEPRIORITISED.
- instant-EV is rank ~1 in every variant. Hint speed is the whole game.

### mock_server_v2.py — BUILT and tested end-to-end
- Wraps `dd_engine.Market` + full agent population, serves the REAL schema (untyped first snapshot,
  shared_update every 0.5s with incremental trades as strings, nested positions w/ volume+cash,
  orders with `id`, `message` acks incl. rejections, `alias_already_exists`, `invalid_exchange`
  when `DD_GAME` set, settlement via instrument `expired`/`expiration_value`). MKT1 present but empty.
- Env: DD_PORT DD_SPEED DD_DURATION DD_SEED DD_DRIFT DD_GAME DD_FEEDERS DD_BATCH, plus
  `DD_HINT_FILE` (off-socket event feed as JSONL) and `DD_HINTS=1` (notification frames — NOT real).
- E2E: `DD_SPEED=10 DD_DURATION=600 DD_BATCH=1 python3 mock_server_v2.py` + mm_bot default config
  ⇒ **0 schema warnings, rank 78/78 (-704)** — matches the sim's verdict on fade-MM.

### mm_bot fixes found by the mock
- **`validate()` treated the real nested positions + `message` frames as schema warnings**, and the
  warning text embedded cash values (a new "unique" warning every fill) ⇒ max-warn kill switch
  tripped after ~10 fills. Fixed: flattens nesting, accepts `volume`, warns on key names only,
  handles `message` acks (prints rejections).
- `mark_pnl()` now uses server `positions.cash` when present (was off by ~126 in the test).

### BIG FINDING for the hint-parser plan
**There are NO notification/event frames anywhere in the 27k captured WS frames** (types seen:
shared_update, user_update, message, error, untyped snapshot). Event text reached players
off-socket (screen/projector/UI elsewhere). So "bot parses hint text from the feed" is impossible
as stated; the parser needs a separate text source (screen OCR, page scrape, or fast manual paste
into a parser that does the math). Need to establish WHERE hints appeared before building it.

### Wash-trading / feeders — status corrected
User clarified: not a hard constraint — it was something they were asked to test hypothetically.
Currently OFF. Sim already models it hypothetically via `feeders=N`. Nothing coordinated is built;
check event rules / organisers before any live use.

---

## SESSION 3b — hint-free strategy (B+C+D) built; E studied in theory

### Real-tape evidence (back half, 8 min, 45→77, settle 78)
- Aggressor-rank signal: top-3 leaders' aggressive prints are followed by +4.8 in their favour at
  30s / +4.7 to settle; bottom-20's by -1.9 / -0.9. Confounded by the trend (everyone's buys looked
  good) — real separation is on sells (top3 -1.3 vs rest -7.3).
- Trade bursts: no edge beyond baseline. Top-3 book imbalance >0.5: +3.65 at 30s (vs ±1.1
  baseline), short-horizon only (nothing to settle).
- Copy-top3 crossing: +174 in 8 min ≈ buy&hold 5 (+165). Passive-join execution LOSES (misses the
  fills that matter, keeps the adverse ones). Same in sim.
- Winners: Colin 902 (46 trades, 1 flip), HenryWang 790 (14 trades, 79% passive fills, 0 flips).
  aleksei -1191 (306 trades, 17 flips). Path travelled 98 pts to net +20 ⇒ winners re-traded every
  event; "go long and hold" lesson was incomplete. Churn is what loses.

### Sim bench (circular for copy-leader: sim leaders ARE the informed agents)
copy-top1 cross budget 6/min: rank 7.5, top5 57%. copy-top3: rank 11-16. With 5 feeders in the
field: rank 42 (fragile to a churny field). Budget must be ≥3/min (a flip = 10 fills).

### Built: `mm_bot.py --strategy copy --copy-k 1 --budget 6 --size 1`
- `ingest_trades` (dedupe by trade_id, aggressor = buyer if bid else seller), `lb_names` from the
  leaderboard, `act_copy` (cross to ±limit in the leader's direction, hold until opposite signal,
  no resting quotes, fill-rate cap). Hot-reloadable: strategy, copy_k, budget.
- NOTE budget is wall-clock minutes: at DD_SPEED=10 use --budget 60.
- D (two aliases) e2e on mock_server_v2: copy1 rank 16 (+196), copy3 rank 17 (+174), 0 warnings.
- Dependency: `configuration.leaderboard_visible` and buyer/seller on prints. Check in the first
  30s of the next event; if hidden, fall back to flat.

### E — self-dealing ring, SIM ONLY (user asked for the theoretical max)
- "Blatant" (rest bid 0 / ask 100) does NOT work: price-time book, you cannot pick a counterparty.
- Working mechanism = flash quote strictly inside the spread + feeder round-trips through it:
  transfer = inside spread × trips. 2 feeders @0.3/step (72 main trades/min): +1591, rank 1 in
  100% of runs; 4 @0.2: +2145, 100%. Below ~20 trades/min: 17-23% rank-1.
- Unknown: server order rate limit. Visible: every main counterparty is an own alias.
- User stance: not a hard constraint, "no one is checking". Claude's stance: analysis done, will
  not build the live ring (self-dealing fraud on the organiser/field at a recruiting event).

### Standing recommendation
A (user keys event type → bot sweeps to EV and rests) is the highest-win% legit strategy (sim rank
1.0-1.2, top5 100%); A+B combined is the strongest. Scripts: scratchpad copy_real.py, sweep_real.py,
hf_sim.py, e_sim.py, e_flash.py (session-local; logic is in mm_bot/agent_sim).

## What's DONE (session 2)

### 1. Zero-sum agent engine — `bot/dd_engine.py` (NEW, the core)
- Price-time-priority limit order book, tick 1, prices [0,100], position limit 5, 1 lot/print
  (matches real feed: 99.6% of trades were volume 1).
- Value process: base = word letter-sum (LEAF=24), 15 events. Deterministic add/pct move fair
  at announce; COIN events (x2 / x0.5) move public EV by x1.25 at announce and RESOLVE later,
  so settlement != final EV (models the real settlement variance).
- Every PnL is a transfer between participants ⇒ genuinely zero-sum (verified: mean ≈ 0).
- Calibrated opponent agents: FastInformed, SlowInformed, Maker (skilled=pull+standaside+
  ev_aware vs naive=stale donor), Noise, Churner, Idle, + optional whale "feeder" churners.
- Strategies-under-test plug in as `Participant` subclasses, same API as agents.

### 2. Bench / calibration harness — `bot/agent_sim.py` (NEW)
- `python3 agent_sim.py calibrate [runs]`  — agent-only leaderboard vs real distribution.
- `python3 agent_sim.py bench [runs]`      — drops each candidate strategy in, reports mean
  rank out of 77, win%, top5%, split by UP vs DOWN rounds.
- `DD_FEEDERS=5 python3 -c "import agent_sim; agent_sim.calibrate(30, feeders=5)"` reproduces
  the INFLATED event-day leaderboard.

## Key findings (validated)

- **Fade-MM (our live config) ranks 75/77 in the sim** — independent confirmation our last-place
  finish was the strategy, not bad luck.
- **maker-only: +93 mean, rank ~23, wins 88% of rounds (both up AND down).** Robust, safely
  upper-third, but NOT a podium strategy.
- **follow-maker as written: +6, rank ~37 — no better than flat. Needs a real signal or drop.**
- **instant-EV (perfect, instant hint-reading): rank 3, top5 98%.** The ONLY thing that wins.
  We could not do this live (hints late + by hand). ⇒ the real lever to WIN is a bot that
  PARSES THE HINT TEXT ITSELF and reprices in milliseconds, not user-typed fairs.
- **Fresh 0 ≈ rank 40 (median).** Doing nothing already beats the losing half.

## Calibration state

NATURAL field (default, un-fed — what a real field looks like WITHOUT our 5 feeder bots):
```
stdev 241 | top5 [959,824,454,399,351] | worst -491 | flat 17% | profitable 38% | zero-sum ✓
```
EVENT field (`feeders=5`, reproduces the day we fed the winners): stdev ~305, top ~950, worst ~-1040.

### KNOWN GAP (next session, first thing)
- **Top end runs hot: 2 FastInformed agents clear ~890 even in the natural field; user says the
  un-fed natural top should be ≈ +400.** The "perfect informed" agents extract too much from the
  slow/maker/noise donor pool. Does NOT change strategy *rankings* (we bench on rank), but the
  absolute top number is inflated ~2x. To fix: weaken/err-up the fast cohort, or make donors lose
  less (makers pull faster / noise smaller), until natural top5 ≈ [400,300,220,170,140], worst ≈ -400.
- Secondary: flat% 17 vs target 10, profitable% 38 vs 44 — minor, lift by nudging slow cohort
  slightly less negative.
- DO NOT overfit to the single partial round. Sim is a filter for BAD ideas, not proof of good ones.

## STILL TODO (step 2 not finished)

- **Rewire `bot/mm_bot.py` opponent sim**: `mock_server.py` still uses the OLD schema/crowd.
  Build `mock_server_v2.py` that wraps `dd_engine.Market`, drives its agents each step, and
  serves the REAL schema so the live bot is tested end-to-end:
  - order_book levels `{bid|ask: vol, price: float}`
  - positions NESTED `[[{user_id,instrument_id,volume,cash}]]`  (THE bug that blinded us live)
  - trades price as STRING; `aggressor_side`, `buyer`, `seller`, `trade_id`
  - order ack `{type:"message", message:{success,description,order_id}}`
  - leaderboard `[[alias,pnl]]`, rank via `leaderboard_position`
  - bot connects with `basic.<b64url(alias:pw)>`, empty pw OK; connect frame
    `{type:"connect",message:{id,username,game_id}}`
  - mm_bot ingest refs: see `mm_bot.py` ingest_shared (~L460) / ingest_user (~L470-507).
- Add a **maker-only mode** to mm_bot (quote-only, pull on jump, inventory skew, NO taker crossing).
- Build the **hint-parser** path (the actual win lever): read event text → update EV in ms → the
  instant-EV strategy becomes reachable. This is what separates rank-23 from rank-3.
- Capture discipline next event: **join observer the instant the exchange opens** (we lost the
  first ~14 min / 1771 trades this time), and launch every bot with a **FIXED uid** per alias.

## Data facts (so nobody re-derives them)
- We have: RAIN trade_ids 74-87 (the open) then 1859-3023 (dense back half). **Missing 88-1858
  (1771 trades, ~14 min, price climbed 26→66) — UNRECOVERABLE.** Bot logs are summaries only
  (no raw frames); the bigger /tmp capture only extends LATER (to 20:34, through settlement).
- Best capture now archived: `frames/dd_capture_all_2034.jsonl` (27325 frames, through settlement,
  only 17 trades missing in the back half). Settlement RAIN = 78.
- Real leaderboard was zero-sum, inflated by our feeders: winner 952, worst -1040 (= our aleksei,
  303 trades, the top churner/feeder). Un-fed natural spread ≈ ±400 (per user).
- Fair-path context (NO prediction — direction changes every round): quiet baseline (median
  tick move 1) + ~26 jumps ≥4pt, balanced 13 up/13 down, frequent reversals (runs mostly len 1,
  max 4), wide range 26-84. Net direction is UNKNOWABLE per round; sim randomizes drift sign.

## Wash-trading / feeding (status per user, session 3)
Not a hard constraint — user was asked to test it hypothetically. Currently OFF; only the sim's
`feeders=N` models it. Check the event rules before anything coordinated goes live.

## SESSION 3c (end of session)
- Added `bot/feeder_poc.py` (sandbox-only self-dealing study: win rate vs ring throughput; 100% rank 1
  at >=120 orders/min with 2 feeders) and `bot/feeder_detect.py` (flags rings from a public trade log;
  flags the sim ring, real RAIN tape = clean). Write-up: `E_WRITEUP.md`.
- Live rate-limit evidence: no throttle/rejection seen; ~6/s per bot sustained, peaks 10-12/s,
  ~30/s combined. Lower bound only.
- `dd_engine.make_population(..., naive_coin=0.0)`: optional share of agents that price coin events
  at x1.0 instead of x1.25. Default 0 = calibration unchanged. Not yet used in a bench.
- Two planned pieces could not be completed this session: a human-speed slide-reader strategy in
  agent_sim, and a real-tape study of post-event repricing speed / first movers. Neither exists yet.

---

## SESSION 4 (read this first — supersedes the field-speed calibration above)

### Q2 answered: the real field reprices ~3x faster than the session-3 sim
- Real tape (back half, `reprice/jumps.py`): 9 jumps of 4 points or more in 8 min. From the first
  move, 50% of the jump is printed in a median of 1.5s and 90% in 8.5s. The first 3 aggressors are
  spread across ~20 different names (Alston_Yam, Mozzarella, Jakob, Edward, Mohijit...), mostly
  1-lot each and within 0-2s of each other. There is no single dominant fast bot; many humans
  react to the same slide at about the same time.
- Same detector on the session-3 sim (`reprice/simjumps.py`): 8.5s / 20s. Too slow.
- New `make_population(speed=...)` / env `DD_FIELD_SPEED` divides the fast, slow and maker
  reaction delays. Speed 3 gives sim 3.5s / 8.75s (r90 matches). Speed 5 gives 2.5s / 5s.
  **Default is now 3.0.** Set `DD_FIELD_SPEED=1` to reproduce the session-3 numbers.
- Calibration at speed 3 (30 runs): top5 [649,531,342,280,256], worst -421, stdev 172, zero-sum.
  The top is closer to the ≈+400 natural target than before (712). Speed 5 is about the same.
- Caveat: only 9 jumps, and the slide time itself is not on the tape, so "onset" is the first
  reaction. The announce→first-print gap in the real event is unknown.

### Q1 answered: slide reader vs copy-top1 (`python3 agent_sim.py slide 40`)
Rank out of 77, 40 runs each. "slide" = sweep to EV after the delay, then keep taking anything
past target ±2. "sweep-once" = same, but only trades within 2s of a fresh read, then holds.
```
                    speed 1 (old)   speed 3 (new default)   speed 5
instant-EV               1.0            1.1                    1.6
slide 1-2s               1.0            1.0                    1.1
slide 3-6s               1.2            9.3                   66.9
slide 5-10s              2.4           73.3                   77.0
sweep-once 1-2s          1.0            1.1                    1.2
sweep-once 2-4s          1.1            1.4                    9.2
sweep-once 3-6s          1.2            9.2                   30.6
sweep-once 5-10s         1.9           35.6                   46.5
copy-top1 b6            16.6           28.4                   17.2
```
- **Reaction time is the whole edge.** At realistic speed you need ≤2-4s from slide to order to
  stay rank ~1. A human reading the slide and typing (3-6s) lands rank 9-30, and 5-10s is mid-pack
  or worse.
- **Continuous taking against a stale target is a disaster.** In the gap between the slide and the
  read, the "slide" variant keeps buying from faster traders at the old EV (rank 73-78 when late).
  Sweep-once removes that tail. Any live slide tool must trade only right after a fresh input, and
  must stop the moment the price moves away from the last target without new input.
- Copy-top1 stays rank ~17-28 with a 65-85% win rate across speeds. It is the robust hint-free
  fallback but not a winner.

### Q3 answered: coin mispricing barely matters
`agent_sim.py slide 40 0.5` (half the field prices coins at ×1.0) at speed 1: changes are within
noise for fast readers (5-10s: rank 2.4→2.0, 8-15s: 23→15). Not worth building around.

### Implication for the next event
Plan A (user keys the event type → bot computes EV → sweeps) only wins if keying is ~1-2s:
pre-made one-key buttons per event type, not free typing. Sweep once, then hold. Use copy-top1 as
the fallback if hints can't be keyed fast enough.

Files: `agent_sim.py` (StratSlideReader, slide_cls, StratCopyTop1, `slide` mode),
`dd_engine.py` (speed knob), `event_data_2026-10-23/reprice/` (tape.py, tape.json, jumps.py,
simjumps.py, result txts).
