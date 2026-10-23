# Delta Dash — offline bot dev stack

Real events are only intermittently live and (when checked) frozen, so this is a
local simulator + bot that speak the reverse-engineered Delta Dash protocol. The
bot is **drop-in** for the real event — only `--url` (and `--game`) change.

Protocol/mechanics: see memory `delta-dash-protocol`. Security assessment: memory
`delta-dash-assessment`.

## Files

- `mock_server.py` — local exchange on `ws://localhost:8765/ws`. Speaks the real
  protocol (`basic.` subprotocol, `connect`/`insert_order`/`delete_order`,
  `shared_update`/`user_update`). Matching engine with price-time priority,
  position limit (5), `orderVolumeLimit` (200), tick/bounds validation,
  settlement to a hidden `expiration_value`. **Seeded from a real captured order
  book** (`seed_book.json`) and driven by noise traders whose fair value
  random-walks → the book moves and trades print.
- `bot.py` — fair-value market maker. Quotes `--edge` ticks around book mid,
  size `--size`, inventory-skews to revert toward flat, respects the position
  limit. Starter strategy to iterate on.
- `seed_book.json` — real MKT1/RAIN book from the leak capture.

## Run

```bash
# terminal 1 — start the mock
python3 mock_server.py

# terminal 2 — run the bot against it
python3 bot.py --url ws://localhost:8765/ws --alias mybot --edge 2 --size 2
```

`--edge 0` quotes at mid (aggressive, gets filled); higher `--edge` earns more
spread but fills less.

## Against the real event (when one is live)

```bash
python3 bot.py --url wss://delta-dash-eur.optibook.net/ws \
    --game <exchange_id> --alias mybot
```

Needs a valid `exchange_id` (join link from the event). Same code path as the mock.

## Validated

Local run: bot connects, quotes both sides, trades, tracks position within ±5,
mean-reverts inventory, reaches rank 1 on the PnL leaderboard, no protocol errors.

## Next

- Tune strategy: better fair value (microprice, order-book imbalance, trade-flow
  drift toward settlement), adverse-selection control, inventory cost.
- `replay.py` backtest: record a mock round to JSONL, replay strategies over it
  with a fill model. (Real captured data is a single static frame — no dynamics —
  so backtests run on mock-generated or future live rounds.)
- Multi-instrument once real events expose more than MKT1/RAIN.
