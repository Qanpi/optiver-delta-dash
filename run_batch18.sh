#!/usr/bin/env bash
# Run N fast rounds of the mock with our bots; collect the RESULT json lines.
# Usage: ./run_batch.sh [N] [SPEED]
set -u
N=${1:-20}; SPEED=${2:-8}; PORT=8799
OUT=/tmp/dd_batch18.jsonl; : > "$OUT"
cd "$(dirname "$0")"
for r in $(seq 1 "$N"); do
  lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null
  DD_BATCH=1 DD_SPEED=$SPEED DD_PORT=$PORT python3 -u mock_server.py > /tmp/dd_mock.log 2>&1 &
  MOCK=$!; sleep 1.0
  DD_SPEED=$SPEED python3 -u ev_bot.py --url ws://localhost:$PORT/ws --alias evbot --size 2 > /dev/null 2>&1 &
  EV=$!
  DD_SPEED=$SPEED python3 -u mm_bot.py --url ws://localhost:$PORT/ws --alias mmbot --size 2 > /dev/null 2>&1 &
  MM=$!
  # wait up to ~25s for the mock to settle + exit
  for _ in $(seq 1 50); do kill -0 $MOCK 2>/dev/null || break; sleep 0.5; done
  kill $EV $MM $MOCK 2>/dev/null
  grep '^RESULT ' /tmp/dd_mock.log | tail -1 | sed 's/^RESULT //' >> "$OUT"
  echo "round $r done"
done
lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null
echo "=== wrote $(wc -l < "$OUT") rounds to $OUT ==="
