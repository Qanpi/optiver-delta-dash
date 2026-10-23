#!/usr/bin/env bash
# Sweep mm_bot --lean (win-seeking trend lean) at fixed size/edge, mmbot alone
# vs the agent population. config = "size edge take lean".
set -u
ROUNDS=${1:-15}; SPEED=${2:-8}; PORT=8822
OUT=/tmp/dd_lean.jsonl; : > "$OUT"
cd "$(dirname "$0")"
CONFIGS=(
  "2 1.0 1.0 0.0"
  "2 1.0 1.0 0.5"
  "2 1.0 1.0 1.0"
  "2 1.0 1.0 1.5"
  "2 1.0 1.0 2.5"
)
for cfg in "${CONFIGS[@]}"; do
  set -- $cfg; SIZE=$1; EDGE=$2; TAKE=$3; LEAN=$4
  TAG="lean${LEAN}"
  for r in $(seq 1 "$ROUNDS"); do
    lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null
    DD_BATCH=1 DD_SPEED=$SPEED DD_PORT=$PORT python3 -u mock_server.py > /tmp/dd_lean_mock.log 2>&1 &
    MOCK=$!; sleep 1.0
    DD_SPEED=$SPEED python3 -u mm_bot.py --url ws://localhost:$PORT/ws --alias mmbot \
        --size "$SIZE" --edge "$EDGE" --take-ticks "$TAKE" --lean "$LEAN" > /dev/null 2>&1 &
    MM=$!
    for _ in $(seq 1 60); do kill -0 $MOCK 2>/dev/null || break; sleep 0.5; done
    kill $MM $MOCK 2>/dev/null
    line=$(grep '^RESULT ' /tmp/dd_lean_mock.log | tail -1 | sed 's/^RESULT //')
    [ -n "$line" ] && echo "{\"cfg\":\"$TAG\",\"lean\":$LEAN,\"r\":$line}" >> "$OUT"
  done
  echo "config $TAG done"
done
lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null
echo "=== wrote $(wc -l < "$OUT") rows ==="
