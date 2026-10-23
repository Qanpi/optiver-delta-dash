#!/usr/bin/env bash
# Robustness stress test: mm_bot at lean 0.0 vs 1.0 across behavioral regimes.
# Shows whether the trend-lean is fragile to wrong assumptions.
set -u
ROUNDS=${1:-10}; SPEED=${2:-8}; PORT=8833
OUT=/tmp/dd_robust.jsonl; : > "$OUT"
cd "$(dirname "$0")"
REGIMES=(lag overshoot efficient random mixed)
LEANS=(0.0 1.0)
for RG in "${REGIMES[@]}"; do
  for LEAN in "${LEANS[@]}"; do
    for r in $(seq 1 "$ROUNDS"); do
      lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null
      DD_BATCH=1 DD_SPEED=$SPEED DD_PORT=$PORT DD_REGIME=$RG python3 -u mock_server.py > /tmp/dd_robust_mock.log 2>&1 &
      MOCK=$!; sleep 1.0
      DD_SPEED=$SPEED python3 -u mm_bot.py --url ws://localhost:$PORT/ws --alias mmbot \
          --size 2 --edge 1.0 --take-ticks 1.0 --lean "$LEAN" > /dev/null 2>&1 &
      MM=$!
      for _ in $(seq 1 60); do kill -0 $MOCK 2>/dev/null || break; sleep 0.5; done
      kill $MM $MOCK 2>/dev/null
      line=$(grep '^RESULT ' /tmp/dd_robust_mock.log | tail -1 | sed 's/^RESULT //')
      [ -n "$line" ] && echo "{\"regime\":\"$RG\",\"lean\":$LEAN,\"r\":$line}" >> "$OUT"
    done
    echo "regime=$RG lean=$LEAN done"
  done
done
lsof -ti tcp:$PORT | xargs kill -9 2>/dev/null
echo "=== wrote $(wc -l < "$OUT") rows ==="
