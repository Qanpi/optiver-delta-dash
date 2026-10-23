# Delta Dash — Live Event RUNBOOK

Recovery doc. If you are a fresh session taking over: read this top to bottom, then
you can run the event without re-deriving anything. Working dir: this folder
(`/Users/alekseiterin/Documents/optiver-trading-game/bot`).

---

## 0. The decision (settled on data)

- **Run ONE market-maker bot** (`mm_bot.py`). It is an ~85% favorite in a 50-player
  sim (treat as a ceiling — a quant firm's candidates may be sharper than our sim crowd).
- **Hint-blind:** it trades the order book only. It never needs the slide hints.
- **No EV bot live** — EV needs the hints, which live are on slides, not in the feed.
- **No second entry needed** — at ~50 players a 2nd MM does not beat one (cannibalizes).
- **NO feeding / wash trading. Ever.** Transferring PnL between your own accounts is
  market manipulation — illegal in real markets, instant red flag at an Optiver event.
  This is a hard line regardless of how the local rules are worded.

## 1. How to launch (the whole thing)

You need two things from the user at game time:
1. the **game_id** = the UUID in the join link `https://.../hub/<game_id>/create`
2. the **alias** to use

Then, from this folder:

```
./run_live.sh <game_id> <alias> [password]
```

- Password is almost certainly empty (the alias-only join — confirmed accepted live).
  Only pass arg 3 if the connect returns `invalid_credentials` (not `invalid_exchange`).
- `run_live.sh` starts, under one `caffeinate` (Mac can't sleep), BOTH:
  - `mm_bot.py` (the trader) — config: `--size 2 --edge 1.0 --take-ticks 1.0 --lean 0.5
    --max-loss 150 --max-warn 10 --nopx-kill 20`, with telemetry + hot-control + reconnect.
  - `supervisor.py` (the watchdog) — defensive only.
- It launches them under `nohup`, so they survive this session dying.

**To relay status to the user's phone instead of the blocking `tail`:** launch the two
processes in the background yourself (same flags as in `run_live.sh`) and read:
- `/tmp/dd_status_<alias>.txt`  — human-readable rank/pnl/pos/mode (watchdog writes it)
- `/tmp/dd_state_<alias>.json`  — raw telemetry
- `/tmp/dd_live_<alias>.log`    — bot summaries + any errors

## 1b. Two instruments (test + RAIN) — trade RAIN only

The live game has a **test instrument** and **RAIN** (the real, scored one). We launch with
`--skip-test` so the bot ignores any instrument flagged `is_test` and trades the rest (RAIN).
On connect, verify in `/tmp/dd_state_<alias>.json`:
- `instruments` shows each id with its `is_test`/`mid`.
- `trading` lists ONLY the real instrument (RAIN).

If the test instrument is NOT flagged `is_test` (so `trading` shows both), pin RAIN by id:
```
echo '{"only": ["RAIN"]}' > /tmp/dd_ctrl_<alias>.json    # use RAIN's actual id from `instruments`
```
(`only` overrides everything; empty `[]` = trade all active again.)

## 2. First 15 seconds after launch — WATCH FOR

The one thing never tested live is **sending orders**. On connect, confirm:
- Bot prints `[*] MM bot ... LIVE (sending orders) [reconnect]`.
- `frames=` count climbs (feed decodes) and `rank=` populates (leaderboard parses).
- Positions start changing (fills happen).
- **No burst of `[server error]` lines.**

If you see errors on every order / no fills / rank never populates → the real order or
feed schema differs from our mock (see §4).

## 3. Live control — change the bot WITHOUT dropping the connection

Write JSON to the control file; the bot hot-reloads it next frame. Example:

```
echo '{"safe": true}'               > /tmp/dd_ctrl_<alias>.json   # go defensive now
echo '{"lean": 0.0}'                > /tmp/dd_ctrl_<alias>.json   # kill directional bet
echo '{"edge": 3.0}'                > /tmp/dd_ctrl_<alias>.json   # widen quotes (less adverse selection)
echo '{"order_throttle": 0.3}'      > /tmp/dd_ctrl_<alias>.json   # slow re-quotes (server rate-limiting)
echo '{"max_loss": 300}'            > /tmp/dd_ctrl_<alias>.json   # raise/relax the kill floor
echo '{"size": 1}'                  > /tmp/dd_ctrl_<alias>.json   # shrink exposure
echo '{"safe": false, "lean": 0.5}' > /tmp/dd_ctrl_<alias>.json   # undo defensive, resume normal
echo '{"stop": true}'               > /tmp/dd_ctrl_<alias>.json   # stop the bot cleanly
```
Hot-reloadable keys: `lean, edge, size, max_loss, safe_edge, order_throttle, limit, vol_limit,
nopx_kill, a_slow, a_fast, jump_k, take_ticks, only, skip_test, safe, stop`.

### If the game settles after every hint/round (not once at the end)
The bot won't crash (it handles new instruments by id, skips expired). But:
- carrying inventory into each settle = gambling on that hint -> run flat: `{"lean": 0.0}`.
- short rounds starve the anchor -> speed it up: `{"a_slow": 0.15}` (or arb-lean via higher `take_ticks`).
Diagnose on connect: watch how often instruments flip `expired` and new ids appear. If frequent,
push `{"lean": 0.0, "a_slow": 0.15}`. If MULTIPLE instruments trade at once and you see `limit`
rejects, the limit may be AGGREGATE across instruments -> lower `{"size": 1}`.

## 4. Troubleshooting (failure modes, in likelihood order)

| symptom | cause | fix |
|---|---|---|
| orders all error, no fills | real order schema ≠ mock (field names / `side` values `bid/ask` vs `buy/sell`) | capture a few frames (`--capture /tmp/f.jsonl`), diff the real `insert_order`/book shape, patch `act()`/`ingest_*`, relaunch (reconnect is seamless) |
| `rank`/`frames` never populate | feed decode/ingest mismatch | inspect `--capture` output; fix `decode()`/`ingest_shared` |
| `invalid_exchange` after START | runtime game_id ≠ URL uuid | read real id off the page: `sessionStorage` / the connect frame in the browser |
| `invalid_credentials` | event issued a password | pass it as arg 3 to `run_live.sh` |
| `session_displaced` | same alias joined twice | DON'T browser-login with the bot's alias; use a different name to watch |
| many `[server error]` on inserts | order rate-limit | `{"order_throttle": 0.3}` (raise until errors stop) |
| bot bleeding in a strong trend | adverse selection | watchdog auto-goes `safe`; or force `{"safe": true}` |
| kill-switch fired wrongly | too-tight floor | `{"max_loss": 300}` or higher; it uses the server's authoritative pnl |

## 5. The safety nets (already built in)

- **Auto-reconnect:** a dropped socket never ends the game; position is server-side and survives.
- **Kill-switch:** halts + flattens if server PnL < −`max_loss` (150), or schema warnings flood,
  or an instrument stays un-priceable. Uses the **authoritative server pnl** (not the rough proxy).
- **Watchdog (`supervisor.py`):** only on sustained evidence (losing + falling + bottom-half rank,
  past warmup, confirmed 2× ) it de-risks IN-PROCESS: step 1 `safe` mode (flat + wider quotes, keeps
  arb+snipe), step 2 cut size. Never touches the connection. Logs every action to
  `/tmp/dd_watchdog_<alias>.log`.
- **Crash-res/defensive parsing:** a malformed frame is skipped, not fatal. `limit`/`vol_limit` are
  read from `configuration` live. Leaderboard parse tolerates list-or-dict rows.

## 6. Protocol facts (confirmed)

- WS: `wss://delta-dash-eur.optibook.net/ws`, subprotocol `basic.<base64url(alias:password)>`.
- connect frame: `{type:"connect", message:{id, username:<alias>, game_id}}`.
- order: `insert_order {user_id, instrument_id, side:"bid"|"ask", price, volume}`;
  `delete_order {user_id, instrument_id, order_id}`.  (side values UNCONFIRMED live — watch §4.)
- `user_update` carries `positions`, `orders`, `configuration.limit`, `leaderboard`,
  `leaderboard_position`. `shared_update` carries `instruments`, `order_books`, `leaderboard`.
- Frames are JSON text OR zlib-deflated binary (`decode()` handles both).
- Empty password is accepted (confirmed live: got `invalid_exchange`, not `invalid_credentials`).

## 7. File map

- `mm_bot.py` — the live bot (THE one we run). Flags: `--help`.
- `supervisor.py` — the watchdog.
- `run_live.sh` — launches bot + watchdog under caffeinate + nohup.
- `mock_server.py` — local exchange for testing. Env: `DD_SPEED`, `DD_PORT`, `DD_REGIME`
  (lag/overshoot/efficient/random/mixed), `DD_AGENTS` (field size), `DD_BATCH` (print RESULT + exit).
- `ev_bot.py`, `naive_bot.py`, `hand_bot.py` — baselines (NOT used live).
- Sweep scripts + `/tmp/agg_*.py` — the experiments behind the decision.

## 8. Measured results (why we chose this)

- Single MM vs crowd: ~85% win at 50 players (+420 avg), ~73% at 18.
- EV (perfect hints): 18% — loses to hint-blind MM. Naive threshold: 0% win. Hand playbook: 0% win,
  mid-pack. Avg human loses money.
- 2nd independent MM: helps at 16 agents (60→70%), NOT at 50 (85 vs 82, tie). So: one bot.
- `lean 0.5` chosen over 0/1.0: best in the realism regime, bounded downside.

## 9. Integrity (non-negotiable)

Bots are allowed (per the user's read of the rules). One honest entry trading the real market.
**No feeding, no wash trading, no sockpuppet multi-aliases to transfer PnL.** If asked, decline and
say why: it's manipulation and the worst possible signal to a market-maker recruiter.
