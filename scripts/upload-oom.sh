#!/usr/bin/env bash
#
# Hammer the n8n webhook with large concurrent file uploads to blow the
# 512 MB heap on the main (web) node.
#
# The `upload-oom` workflow must be imported AND active first, so that
# POST http://localhost:8080/webhook/upload is live.
#
# Tunables (env vars):
#   URL          webhook endpoint         (default: prod webhook below)
#   SIZE_MB      payload size per request (default: 200)
#   CONCURRENCY  parallel uploads/round   (default: 8)
#   ROUNDS       number of rounds         (default: 50)
#
# Example:
#   SIZE_MB=250 CONCURRENCY=10 ROUNDS=100 ./scripts/upload-oom.sh
set -euo pipefail

URL="${URL:-http://localhost:8080/webhook/upload}"
SIZE_MB="${SIZE_MB:-200}"
CONCURRENCY="${CONCURRENCY:-8}"
ROUNDS="${ROUNDS:-50}"
FILE="${FILE:-./.oom-payload.bin}"

if [ ! -f "$FILE" ]; then
  echo ">> generating ${SIZE_MB}MB payload at $FILE"
  head -c "$((SIZE_MB * 1024 * 1024))" /dev/zero > "$FILE"
fi

echo ">> target      : $URL"
echo ">> payload     : ${SIZE_MB}MB ($FILE)"
echo ">> concurrency : $CONCURRENCY   rounds: $ROUNDS"
echo ">> watch the main node with:  docker compose logs -f n8n-main"
echo

for r in $(seq 1 "$ROUNDS"); do
  for _ in $(seq 1 "$CONCURRENCY"); do
    curl -s -o /dev/null -F "file=@${FILE}" "$URL" &
  done
  wait
  echo "round $r/$ROUNDS complete"
done

echo ">> done (if the main node survived, raise SIZE_MB / CONCURRENCY)"
