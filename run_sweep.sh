#!/usr/bin/env bash
# Sweep mm_bot aggressiveness. For each config run ROUNDS rounds (mmbot alone vs
# the agent population) and log RESULT json tagged with the config.
# Usage: ./run_sweep.sh [ROUNDS] [SPEED]
set -u
ROUNDS=${1:-12}; SPEED=${2:-6}; PORT=8811
OUT=/tmp/dd_sweep.jsonl; : > "$OUT"
cd "$(dirname "$0")"

# config = "size edge take-ticks"
CONFIGS=(
  "2 1.0 1.0"
  "3 1.0 1.0"
  "4 1.0 1.0"
  "5 1.0 1.0"
  "3 0.5 1.0"
  "4 0.5 0.5"
)

for cfg in "${CONFIGS[@]}"; do
  set -- $cfg; SIZE=$1; EDGE=$2; TAKE=$3
  TAG="s${SIZE}_e${EDGE}_t${TAKE}"
  for r in $(seq 1 "$ROUNDS"); do
    lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null
    DD_BATCH=1 DD_SPEED=$SPEED DD_PORT=$PORT python3 -u mock_server.py > /tmp/dd_sweep_mock.log 2>&1 &
    MOCK=$!; sleep 1.0
    DD_SPEED=$SPEED python3 -u mm_bot.py --url ws://localhost:$PORT/ws --alias mmbot \
        --size "$SIZE" --edge "$EDGE" --take-ticks "$TAKE" > /dev/null 2>&1 &
    MM=$!
    for _ in $(seq 1 60); do kill -0 $MOCK 2>/dev/null || break; sleep 0.5; done
    kill $MM $MOCK 2>/dev/null
    line=$(grep '^RESULT ' /tmp/dd_sweep_mock.log | tail -1 | sed 's/^RESULT //')
    [ -n "$line" ] && echo "{\"cfg\":\"$TAG\",\"size\":$SIZE,\"edge\":$EDGE,\"take\":$TAKE,\"r\":$line}" >> "$OUT"
  done
  echo "config $TAG done ($ROUNDS rounds)"
done
lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null
echo "=== wrote $(wc -l < "$OUT") rows to $OUT ==="
