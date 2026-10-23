# Delta Dash — Live Event Post-Mortem (2026-10-23, RAIN event)

Result: near-bottom on all entries. Best = coffeezilla (~rank 34/79, roughly flat).
Worst = aleksei (~−1180, last). Root cause: **our strategy was backwards for this market.**

## 1. The core lesson (most important)

- Our MM is a **mean-reversion / fade** strategy: it sells rallies, buys dips, bets price
  snaps back. It won ~85% in sim against a mean-reverting crowd.
- The **real RAIN market TRENDED hard one direction** (24 → 30 → 66 → ... → ~79) driven by
  the events. In a strong trend, a fading MM keeps shorting into the rise and gets run over.
- **We were short almost the entire event while price rose.** That is the whole loss.
- **Winners went LONG and HELD.** The right strategy here was momentum/trend-follow + hold.
- **Flip-flopping was the single biggest money-loser** — every direction change paid the
  bid/ask spread twice. We flipped ~6+ times.
- **Reacting to each event live was a beat too slow** — exactly the problem the hint-blind
  design was meant to avoid, but then we tried to use hints anyway and mistimed every one.

### Takeaways for next time
1. This market is **event-driven and trending**, NOT mean-reverting. Do NOT run a fade-MM.
2. Trade **WITH** the move and **HOLD**. Pick a direction and commit; do not flip.
3. A **fresh entry starts at 0 = instant top-half** (most of the field loses money). Staying
   near flat already beats >50% of players. Don't give that away by over-trading.
4. If using fair-value: it must be the **running EV updated every event**, and you must be
   fast. We were not fast enough — assume you won't be, and prefer trend-follow + hold.

## 2. Real game mechanics (observed)

- **Value = a word's letter-sum** (A=1..Z=26). Event 1 word "LEAF" = 12+5+1+6 = **24** (base).
- Each event modifies the value:
  - multiplier: "50% double / 50% halve" → EV ×1.25
  - additive shock: "+36 points", "major outage" (fall)
  - percentage: "+25%"
- **15 events** total, spaced through the round. Value **trended strongly up** overall this round.
- Strict price caps **0..100**. Position limit **5**. Settles to the final realized value.
- Two instruments: **MKT1** (is_test=true, tick 0.1, practice) and **RAIN** (is_test=false,
  tick 1.0, the scored market). Trade RAIN only (`--skip-test`).

## 3. Real protocol schema (differs from our mock — update the mock!)

- `shared_update` top-level: `instruments`, `order_books`, `trades`, `leaderboard`.
  - instrument: `{id, name, tick_size, paused, expired, expiration_value, is_test, parameters:{lower_bound, upper_bound}}` (matches mock).
  - **order_book levels**: `{"bid": <vol>, "price": <float>}` / `{"ask": <vol>, "price": <float>}` — volume field is `bid`/`ask`, price is a float. (mock used `volume`.)
  - **leaderboard**: `[[alias, pnl], ...]` sorted desc. (matches mock row format.)
- `user_update`: `{configuration:{limit, leaderboard_visible}, orders, positions, leaderboard_position}`.
  - **positions is NESTED**: `"positions": [[ {user_id, instrument_id, volume, cash}, ... ]]` —
    an extra outer list, and the size field is **`volume`** (+ a per-instrument `cash`). (mock
    used a flat `[{instrument_id, position}]`.) THIS was the dangerous bug — fixed in mm_bot.
  - **rank** = `leaderboard_position` (NOT a leaderboard array here).
  - **orders**: `{id, user, user_id, instrument_id, price, volume, side:"bid"|"ask"}` — order id
    field is `id`; side labels `bid`/`ask` (matches what we send).
- **`message`-type frame** = order ack: `{type:"message", message:{success, description, order_id}}`.
  Safe to ignore.
- `trades`: `{instrument_id, price:"<string>", volume, aggressor_side, buyer, seller, trade_id}`
  — note trade **price is a STRING**; book prices are floats.
- Frames are JSON text (this server) — no zlib binary observed live.
- Auth: subprotocol `basic.<base64url(alias:password)>`, **empty password works**.

## 4. Operational lessons

- **`alias_already_exists`**: the server locks an alias to the uid that first registered it.
  To reconnect/resume an alias, reuse the SAME uid (`--uid <uid>`). A new random uid collides.
  -> going forward, launch every bot with a FIXED uid you record, so restarts always resume.
- **Killed bots can't be un-killed via control** (no resume key) — they need a restart. Consider
  adding a `resume` control key + writing uid into telemetry so restarts are clean.
- Fresh aliases start at pnl 0 = mid/top-half instantly.
- Hot-control (fair/lean/edge/size/safe/etc. via the control file) worked well and never
  dropped the connection — that infrastructure is solid.

## 5. What to fix in the sim (`mock_server.py`) for future prep

- Make the settlement value **trend** on events (word letter-sums, multipliers, additive/pct
  shocks, occasional big moves), not mean-revert around a center. Our mock's net-zero-drift
  mean-reverting crowd is why the fade-MM looked great and lost for real.
- Add informed/trend-following agents that push price toward the evolving value, so a fade
  strategy gets adversely selected (as it did live).
- Match the real schema (nested positions w/ volume+cash, message acks, string trade prices)
  so the bot is tested against reality.

## 6. Data saved here
- `frames/` — raw captured frames (full event evolution + real schema). `dd_capture_all_*.jsonl`
  is the complete stream; `live_test_frames.jsonl` is the test-market schema sample.
- `logs/` — every bot's full session log (trades, schema warnings, our reactions per event).
- `states/` — final telemetry per entry.
