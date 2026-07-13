#!/usr/bin/env bash
#
# Capture a V8 heap snapshot from a running n8n node and copy it to the host,
# so you can load it into Chrome DevTools (Memory tab) and see EVERY object
# inside the heap — constructors, retained sizes, string contents, buffers.
#
# Requires NODE_OPTIONS to include --heapsnapshot-signal=SIGUSR2 (set in
# docker-compose.yml). Node writes the .heapsnapshot to the process cwd.
#
# Usage:
#   scripts/heap-snapshot.sh                 # snapshot n8n-main (the web node)
#   scripts/heap-snapshot.sh n8n-worker      # snapshot the worker
#
# Then: Chrome → chrome://inspect (or DevTools → Memory → Load) → open the file
#       from ./heapsnapshots/. Sort by "Retained Size" to see what dominates.
set -euo pipefail

SERVICE="${1:-n8n-main}"
OUTDIR="${OUTDIR:-./heapsnapshots}"
mkdir -p "$OUTDIR"

CID=$(docker compose ps -q "$SERVICE")
if [ -z "$CID" ]; then echo "!! service '$SERVICE' not running"; exit 1; fi

# Find the MAIN n8n node PID inside the container. The heap cap comes from the
# NODE_OPTIONS env (not argv), so we can't grep argv — instead pick the node
# process whose parent is tini/PID 1 (the JS Task Runner is a child of it).
# TARGET=runner overrides this to snapshot the JS Task Runner child instead.
TARGET="${TARGET:-main}"
PID=$(docker compose exec -T "$SERVICE" sh -c '
  best=""
  for pid in $(pgrep node); do
    ppid=$(awk "{print \$4}" /proc/$pid/stat 2>/dev/null)
    rss=$(awk "/VmRSS/{print \$2}" /proc/$pid/status 2>/dev/null)
    if [ "'"$TARGET"'" = "runner" ]; then
      [ "$ppid" != "1" ] && echo "$rss $pid"
    else
      [ "$ppid" = "1" ] && echo "$rss $pid"
    fi
  done | sort -rn | head -1 | awk "{print \$2}"')
if [ -z "${PID:-}" ]; then echo "!! could not find a matching node PID in $SERVICE (TARGET=$TARGET)"; exit 1; fi
echo ">> $SERVICE: node PID $PID (TARGET=$TARGET) — sending SIGUSR2 (writing heap snapshot)…"

# Record which snapshots exist before, so we can find the new one.
BEFORE=$(docker compose exec -T "$SERVICE" sh -c "ls -1 /home/node/*.heapsnapshot 2>/dev/null" || true)
docker compose exec -T "$SERVICE" kill -USR2 "$PID"

echo ">> waiting for the snapshot to finish writing (large heaps take a few seconds)…"
NEW=""
for _ in $(seq 1 40); do
  sleep 1
  AFTER=$(docker compose exec -T "$SERVICE" sh -c "ls -1t /home/node/*.heapsnapshot 2>/dev/null" || true)
  NEW=$(comm -13 <(echo "$BEFORE" | sort) <(echo "$AFTER" | sort) | tail -1)
  [ -n "$NEW" ] && break
done
if [ -z "$NEW" ]; then
  echo "!! no new snapshot appeared. Is --heapsnapshot-signal=SIGUSR2 in NODE_OPTIONS?"
  echo "   current: $(docker compose exec -T "$SERVICE" printenv NODE_OPTIONS)"
  exit 1
fi

# Wait until the file stops growing — a multi-hundred-MB snapshot takes seconds
# to flush, and copying mid-write yields a truncated (or 0-byte) file.
echo ">> writing $(basename "$NEW") …"
last=-1
for _ in $(seq 1 60); do
  cur=$(docker compose exec -T "$SERVICE" sh -c "stat -c %s '$NEW' 2>/dev/null || wc -c < '$NEW'" | tr -d ' \r')
  [ "$cur" = "$last" ] && [ "$cur" -gt 0 ] && break
  last="$cur"; sleep 1
done
echo ">> snapshot size in container: $(awk "BEGIN{printf \"%.1f\", ${last:-0}/1048576}") MB"
BASE=$(basename "$NEW")
DEST="$OUTDIR/${SERVICE}-${BASE}"
docker compose cp "$SERVICE:$NEW" "$DEST"
SIZE=$(du -h "$DEST" | awk '{print $1}')
echo ">> saved $DEST  ($SIZE)"
echo ">> open Chrome → chrome://inspect → 'Load' (or DevTools ▸ Memory ▸ Load) → pick that file"
