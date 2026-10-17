#!/usr/bin/env bash
# Release drill against the local stack, under continuous load:
#   1. baseline traffic on stable v1.4.0
#   2. canary v1.4.1 (good)                        -> eval gate, 10/25/50%, promoted
#   3. canary v1.5.0 (bad), eval gate skipped      -> runtime analysis + alerts, automatic rollback
#   4. canary v1.5.0 (bad), with the eval gate     -> rejected before any traffic
#   5. manual rollback of stable v1.4.1 -> v1.4.0  (`make rollback`)
# Writes a report to docs/results/. Needs `make up` first.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
RPS=${RPS:-3}
BASELINE_S=${BASELINE_S:-180}
STATE=deploy/state
mkdir -p "$STATE"

started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "drill started $started"
$PY -m canary.controller status

$PY -m load.loadgen --rps "$RPS" --duration 0 > "$STATE/drill-load.log" 2>&1 &
LOAD_PID=$!
trap 'kill $LOAD_PID 2>/dev/null' EXIT

echo "== baseline: ${BASELINE_S}s of traffic on stable"
sleep "$BASELINE_S"

echo "== 1/4 canary v1.4.1 (expected: promoted)"
$PY -m canary.controller deploy v1.4.1
echo "exit=$?"

echo "== 2/4 canary v1.5.0 without eval gate (expected: automatic rollback during traffic)"
$PY -m canary.controller deploy v1.5.0 --skip-evals
echo "exit=$?"

sleep 30
echo "== 3/4 canary v1.5.0 with eval gate (expected: rejected before traffic)"
$PY -m canary.controller deploy v1.5.0
echo "exit=$?"

echo "== 4/4 manual rollback of stable (expected: v1.4.1 -> v1.4.0)"
$PY -m canary.controller rollback
echo "exit=$?"

sleep 60
kill -TERM $LOAD_PID 2>/dev/null  # SIGINT is ignored by background jobs of a script
wait $LOAD_PID 2>/dev/null
finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "drill finished $finished"
$PY scripts/drill_report.py --since "$started" --until "$finished"
