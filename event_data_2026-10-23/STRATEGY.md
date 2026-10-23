# Winning strategy — derived + validated from real RAIN data (2026-10-23)

## Proven on the real recorded book (realistic taker fills, time-ordered)
| strategy | PnL | trades |
|---|---|---|
| long-hold (buy5, sit) | +130 | 5 |
| flat / no-trade | 0 | 0 |
| fade-MM (what we ran) | -135 | 235 |
| momentum-chase | -380 | 145 |
(live, with our churn+flip-flops+adverse selection, we actually hit -135 to -1186.)


## ⚠️ CRITICAL: long-hold is NOT the strategy (avoid overfitting to this up-round)
This round trended UP, so long-hold won (+130). In a DOWN round it is the big LOSER.
"Hold" is only the discipline (don't churn); DIRECTION is the edge and must be earned
every round. The strategy is DIRECTION-ADAPTIVE HOLD:
  - direction = net-EV of decoded hints, OR informed order-flow imbalance (proxy).
  - commit that direction, HOLD, few trades; FLAT if the signal is weak/conflicting.
Never assume long. Validate in the sim with BOTH up-rounds and down-rounds:
  - direction-adaptive-hold must win in both.
  - naive long-hold must win up-rounds and LOSE down-rounds (proving it is not robust).

## The strategy
1. DIRECTION from order-flow imbalance (aggressive buys vs sells):
   - organic early flow was 2.4:1 BUY -> LONG, and it was correct (price 49 -> 76).
   - rule: running buy-aggr / sell-aggr ratio; >~1.5 => long, <~0.67 => short, else FLAT.
2. COMMIT the position (~5 trades to reach +/-limit) and HOLD. Do not re-trade.
3. Unclear/balanced flow => stay FLAT (0 = top-half; >50% of players finish negative).
4. NEVER churn. Each trade pays the ~2pt spread AND feeds opponents.

## Hard rules (from what killed us)
- NO flip-flopping (every reversal pays the spread twice; our flips were the top loss).
- NO fade-MM / mean-reversion here — market TRENDS on events, fading gets run over.
- Minimise trade count. We were 27% of ALL market volume (260 trades) = massive feeding;
  the top score (905) was inflated by our donations, so the real beatable bar is lower.
- Entry timing matters: commit EARLY (long-hold from tick0 = +130; entering late captured less).

## Open questions to resolve in the rebuilt sim (mock_server_v2)
- Best flow threshold + warmup for the direction signal (early-but-not-noise).
- When to FLATTEN before settlement vs hold through (the 50/50 double adds settle variance).
- How to detect a genuine trend-reversal (the "outage" dip to 26) vs noise, without churning.

## ‼️ MAJOR CORRECTION (from the full 10MB capture incl. settlement)
Winner = ~902-952 (Colin/HenryWang). RAIN ended 78, capped 0-100, limit 5 -> holding maxes
at ~290. 902 is IMPOSSIBLE by holding. The path had 80 direction changes; perfect swing-MM
ceiling = ~1688 pts. The winner CAPTURED THE SWINGS (~53% of max) via skilled market-making.

REVISED CONCLUSIONS:
- long-hold (+145) = safe top-half, NOT winning (only ~9% of available edge).
- The winning edge = MARKET-MAKE the volatility (harvest the 80 swings), managing adverse
  selection (median ~1pt drag/fill).
- OUR MM strategy was basically RIGHT; we lost on EXECUTION:
  (a) adverse selection, (b) WE destroyed it with manual fair-value flips + directional
  chasing, converting a swing-capturer into a -5-into-rallies directional gambler.
- Biggest self-inflicted wound: manual intervention. A clean, untouched MM likely does far
  better than our meddled -1186.

REBUILD PRIORITY: model ADVERSE SELECTION faithfully (informed flow picks off stale quotes),
then develop an MM that captures swings while minimizing getting picked off (quote-skew, pull
quotes near jumps, size down in uncertainty). Validate: good-MM ~> +500-900; naive-MM/our-meddled
~< 0; long-hold ~+150 (safe, mid-pack). Test across up AND down rounds.

## ✅ CALIBRATED FINDING (markout analysis of real fills)
Passive-maker markout was FAVORABLE: +0.4..+0.9 per fill (organic makers +0.7..+1.0;
including our aleksei it drops to +0.4..+0.65 -> WE under-earned what others captured).
=> Market-making was NET PROFITABLE. The 902 winner just MADE markets over many fills.
=> v2 sim's "naive always-on MM +366" is VALIDATED by the real markout (not miscalibrated).

THE EXECUTION FIX (what actually beats this game):
- BE A MAKER: post resting bid below mid / ask above mid and WAIT. Capture the favorable
  markout (~0.5-0.9/fill) across hundreds of fills.
- DO NOT CROSS/TAKE: our snipe, lean-take, flatten, and every manual fair-flip were TAKER
  actions that pay the spread + eat adverse selection. That converted a winning making game
  into a losing taking game.
- DO NOT MEDDLE: leave the maker running; manual flip-flops are what produced -1186.
- inventory control via quote-SKEW (lean quotes to mean-revert position), never by crossing out.

NEXT: build the maker-only bot (post/rest, skew for inventory, minimal/no taking), validate in
v2 sim (should match ~organic maker +0.7/fill), and stress across up/down + high-vol rounds.

## 🏆 FINAL STRATEGY BAKEOFF (v2 sim, 300 runs, up/down/mixed)
MIXED:  new-MM(maker-only) +71  >  follower +35  >  flat 0  >  long-hold -6
        >> old-MM(fade+take) -377  >>  pure-EV(slow fair) -613
UP:     long-hold +132 (right-dir gamble), new-MM +9, EV -656
DOWN:   new-MM +166 (67% win!), long-hold -147, EV -568

WINNER = MAKER-ONLY MM. Only strategy POSITIVE in BOTH up and down rounds. Direction-neutral
making harvests volatility regardless of trend. Build this; run it; don't touch it.

CONFIRMED LOSERS (what we did live):
- pure-EV (slow fair-chasing) = WORST (-613). This is literally what sank aleksei.
- old fade+take MM = -377.
- long-hold = directional coin-flip (great up, dead down), NOT robust.

BUILD SPEC for the real bot (maker-only mode in mm_bot):
- post tight two-sided quotes ALWAYS; inventory-skew to mean-revert position to 0.
- NO taking (disable snipe / lean-take / flatten-cross / crossed-arb).
- NO hint/fair input, NO manual intervention during the event.
- tune quote width for max fills without blowing inventory limits.
