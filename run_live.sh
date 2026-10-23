#!/usr/bin/env bash
# Live launcher for the Delta Dash event.
#   ./run_live.sh <game_id> <alias> [password]
#
# Starts, under one caffeinate (Mac can't sleep while this runs):
#   1. mm_bot  — the market maker, with auto-reconnect + telemetry + hot-control.
#   2. supervisor — the watchdog: de-risks the bot in-process if it is sustainably
#      losing, WITHOUT ever touching the connection.
# Writes a phone-readable status to $STATUS; tails it so you can watch from the
# Claude app. Ctrl-C (or killing this) stops everything cleanly.
set -u
GAME="${1:?usage: run_live.sh <game_id> <alias> [password]}"
ALIAS="${2:?usage: run_live.sh <game_id> <alias> [password]}"
PW="${3:-}"
URL="wss://delta-dash-eur.optibook.net/ws"
cd "$(dirname "$0")"

STATE="/tmp/dd_state_${ALIAS}.json"
CTRL="/tmp/dd_ctrl_${ALIAS}.json"
STATUS="/tmp/dd_status_${ALIAS}.txt"
BOTLOG="/tmp/dd_live_${ALIAS}.log"
WDLOG="/tmp/dd_watchdog_${ALIAS}.log"
rm -f "$STATE" "$CTRL" "$STATUS"

echo "[*] $ALIAS -> game $GAME"
echo "    bot log:   $BOTLOG"
echo "    watchdog:  $WDLOG"
echo "    status:    $STATUS  (phone-readable)"

# keep the Mac fully awake for the whole session (system + display + disk)
caffeinate -dis &
CAF=$!

cleanup(){ echo "[*] stopping..."; kill $BOT $WD $CAF 2>/dev/null; exit 0; }
trap cleanup INT TERM

# 1) the market maker (nohup = survives this terminal / a dropped session)
nohup python3 -u mm_bot.py \
    --url "$URL" --game "$GAME" --alias "$ALIAS" ${PW:+--password "$PW"} \
    --size 2 --edge 1.0 --take-ticks 1.0 --lean 0.5 \
    --max-warn 10 --nopx-kill 20 --max-loss 150 \
    --state-file "$STATE" --control-file "$CTRL" --safe-edge 3.0 --order-throttle 0 \
    --skip-test \
    > "$BOTLOG" 2>&1 &
BOT=$!

# 2) the watchdog (defensive only; real-time thresholds)
nohup python3 -u supervisor.py --state-file "$STATE" --control-file "$CTRL" \
    --log "$WDLOG" --status "$STATUS" \
    --poll 3 --warmup 30 --window 45 --drop 40 --confirm 2 --safe-edge 3.0 \
    > /dev/null 2>&1 &
WD=$!

echo "[*] running (bot pid $BOT, watchdog pid $WD). Ctrl-C to stop."
echo "============================================================"
# foreground: live status + bot summaries for the phone
tail -n +1 -f "$STATUS" "$BOTLOG"
