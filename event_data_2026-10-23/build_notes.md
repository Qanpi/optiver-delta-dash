# mock_server_v2 — calibration targets (from real RAIN capture 2026-10-23)

## Value process (settlement truth)
- base = word letter-sum (A=1..Z=26). e.g. LEAF=24.
- 15 discrete EVENTS, each jumps the value:
  - multiplier (e.g. 50% x2 / 50% x0.5, EV x1.25)  [REALIZED randomly -> big settlement variance]
  - additive (+/- N points, e.g. +36)
  - percentage (+/- X%, e.g. +25%)
  - occasional shock ("outage" -> big fall)
- value TRENDS (this round net strongly up 24 -> ~76, peak ~84, dip ~26). Randomize net
  direction across runs for robustness, but within a run it should TREND, not revert.
- settlement = final realized value (incl. the double resolving -> winner hit ~905,
  implying a large late up-move). Model a settlement spike.

## Price / book micro-structure
- mostly QUIET between events (median tick-to-tick move = 0), then JUMPS at events
  (~23 moves >5pt over the round, max ~27 in one tick).
- spread: median 2 (tick=1), widens to ~35 right after a jump (dislocation window).
- depth: ~17 bid levels / ~9 ask; more depth on the trend side.
- aggressor flow skews toward the trend (566 buy vs 408 sell aggressions in up-move).

## Agents (must make fading/churning LOSE)
- INFORMED/trend agents: trade toward the evolving value, and lift/hit through a passive
  MM's stale quote right before a jump -> ADVERSE SELECTION is the key mechanic.
- noise agents: random, provide the churn/spread.
- the sim must punish OVER-TRADING: crossing the (median-2) spread 260x = ~hundreds of pts
  bled, matching aleksei's real loss.

## Schema to match real server
- shared_update: instruments, order_books (levels {bid|ask: vol, price}), trades
  (price as STRING, aggressor_side, buyer, seller, trade_id), leaderboard [[alias,pnl]].
- user_update: configuration{limit}, orders [{id,user,user_id,instrument_id,price,volume,side}],
  positions NESTED [[{user_id,instrument_id,volume,cash}]], leaderboard_position.
- order ack as {type:"message", message:{success,description,order_id}}.

## Validation (rebuilt sim must reproduce)
- long-HOLD (buy 5, sit)      -> WINS (~+100-900 depending on realized events)
- fade-MM / over-trader       -> LOSES (churn + adverse selection)
- trend-CHASE (active)        -> LOSES (whipsaw on the quiet-then-jump path)
- top score ~900; median player ~0/negative (most lose).

## REAL LEADERBOARD CALIBRATION (82 players, final)
- ZERO-SUM transfer game: mean ~0, stdev 285. winner 902, top5 [902,790,572,458,445],
  median 0, worst -1191 (= our aleksei, also MOST active at 303 trades -> we funded the winners).
- distribution: 44% profitable, 20% flat, 44% losing. ~22 winners >100, long losing tail.
- INTERPRETATION: a few SKILLED MAKERS extract from many CHURNERS. We were the top churner/feeder.

## HONEST MODEL GAP (must fix for a trustworthy sim)
- v2 sim abstracts opponents as a flow process -> NOT zero-sum -> overstates maker PnL
  (sim maker +71 vs real-replay maker -32; results are fill-model-sensitive).
- FIX = agent-based rebuild: explicit opponent agents (skilled makers + noise churners) whose
  collective PnL sums to zero with ours, calibrated to the distribution above.
- also need a realistic MAKER FILL model (queue priority / which trades hit our quote), since
  that single assumption flips maker-only between +71 and -32.
- validation: sim leaderboard must reproduce winner ~900, stdev ~285, 44/20/44 split, and a
  naive churner (our old config) must finish near the bottom.

## WHAT'S ACTUALLY SOLID (no overclaiming)
- our live strategies (EV/fair-chase, fade+take) were among the WORST; chasing+meddling = -1191.
- disciplined simple play (don't churn, don't chase, don't meddle) ~ mid-pack / top-half, NOT #1.
- winning #1 needs MM EXECUTION skill (placement/queue/sizing) not captured by simple strategies.
