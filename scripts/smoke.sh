#!/usr/bin/env bash
# CI smoke test of the full stack: evals through the gateway, a minute of load,
# then assert that metrics, recording rules and traces all show up.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}

wait_for() {  # url, name
  for _ in $(seq 1 60); do curl -fsS "$1" >/dev/null 2>&1 && return 0; sleep 2; done
  echo "timed out waiting for $2" >&2; docker compose logs --tail 50; exit 1
}
wait_for http://localhost:8080/healthz gateway
wait_for http://localhost:9090/-/ready prometheus
wait_for http://localhost:6006/healthz phoenix

$PY -m evals.run_evals --url http://localhost:8080 --min-pass-rate 1.0
$PY -m load.loadgen --rps 3 --duration 60
sleep 15

prom() { curl -fsS --get http://localhost:9090/api/v1/query --data-urlencode "query=$1" | jq -r '.data.result[0].value[1] // "none"'; }
requests=$(prom 'sum(increase(agent_requests_total[5m]))')
failure_ratio=$(prom 'sum(agent:failure_ratio:rate5m{track="stable"})')
cost=$(prom 'sum(agent_llm_cost_usd_total)')
echo "requests=$requests failure_ratio=$failure_ratio cost_usd=$cost"
[[ "$requests" != none ]] && awk "BEGIN{exit !($requests > 100)}" || { echo "too few requests recorded"; exit 1; }
[[ "$failure_ratio" != none ]] && awk "BEGIN{exit !($failure_ratio < 0.01)}" || { echo "failure ratio too high"; exit 1; }

# Traces reach Phoenix once the collector's tail-sampling window (100s) closes.
for _ in $(seq 1 30); do
  spans=$(curl -fsS "http://localhost:6006/v1/projects/production-agent/spans?limit=100" 2>/dev/null | jq '[.data[] | select(.span_kind=="AGENT")] | length' 2>/dev/null || echo 0)
  [[ "$spans" -gt 0 ]] && { echo "phoenix: $spans agent spans"; exit 0; }
  sleep 10
done
echo "no agent spans in Phoenix"; exit 1
