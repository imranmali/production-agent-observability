# Runbook

One section per alert (the alert's `runbook_url` points here), then the release
procedures. Dashboards: **Agent overview** and **Agent canary analysis** in
Grafana. Traces: Phoenix (or LangSmith / Arize when the collector overlay is on).
Every API response carries `x-trace-id`; paste it into the trace UI's search.

## First five minutes, any alert

1. Is it one track or both? `track` label on the alert, *Failure ratio by track* panel.
   Canary only → `make rollback` (k8s: `kubectl argo rollouts abort support-agent -n support-agent`). Don't debug first.
2. Was there a deploy? Purple deploy annotations on every panel; `make status`.
3. Is it the model provider? *Model call latency p95* and `agent_llm_requests_total{status!="ok"}`; https://status.anthropic.com.
4. Open three failing traces: in Phoenix, filter the traces list to error status; the root span's `agent.outcome` attribute says why each one failed.

## AgentLoopDetected

*info.* At least one request was stopped by the loop detector in the last 10 minutes.
The guardrail already did its job (the customer got a fallback answer, spend was capped).

- Find the traces: error traces in Phoenix whose root span has `agent.outcome = loop_detected`. The `guardrail.tripped`
  event on the root span says which tool was repeated and how.
- One-offs are normal for an LLM agent. Track the rate; escalate if it trends up.

## AgentLoopRateHigh

*critical.* More than 2% of requests on a track loop for 2 minutes.

- If `track=canary`: roll back (`make rollback`). The canary controller also does this on its own.
- If `track=stable` and a deploy happened recently: `make rollback` reverts stable to the previous release.
- If no deploy: something outside the code changed. Check the model (a fallback model serving traffic,
  `agent_llm_fallbacks_total`), tool outputs (a tool returning an error the model keeps retrying:
  *Tool error ratio by tool*), and upstream data (orders/KB).
- Typical root causes: a prompt that tells the model to "re-check" or "verify" without a stopping rule;
  a tool schema change the model can't satisfy; a tool that returns an ambiguous empty result.

## AgentStepBudgetExhausted

*warning.* More than 5% of requests run out of steps (`MAX_STEPS`) without a final answer.
Same investigation as loops: the model is wandering rather than repeating. Look at *Model calls per
request*; compare tool mixes before/after the change. Raising `MAX_STEPS` is rarely the fix.

## AgentFailureRateHigh

*critical.* More than 5% of a track's requests end in a non-success outcome for 3 minutes.
*Failures / s by outcome* tells you which kind:

| outcome | look at |
|---|---|
| `provider_error` | AgentLLMProviderErrors below |
| `loop_detected`, `max_steps` | AgentLoopRateHigh above |
| `token_budget`, `cost_budget` | a prompt or retrieval change sending far more context; *Tokens / s by type* |
| `timeout` | *Model call latency p95*; provider slowness or many steps |
| `refusal` | `agent_llm_refusals_total` by category; content of the failing requests |
| `error` | agent logs (`docker compose logs agent-stable`, JSON lines with `exc`) |

## AgentErrorBudgetFastBurn

*critical, pages.* Both the 1h and 5m error ratios are above 14.4× the budget (14.4% failures).
At this rate the month's 1% error budget is gone in about two days. Treat as an incident;
work through AgentFailureRateHigh. Roll back first if a deploy is in the window.

## AgentErrorBudgetSlowBurn

*warning.* 6h and 30m error ratios above 6× budget. Not urgent, but it will exhaust the
budget in about five days. Ticket it; check *Error budget remaining (30d)*.

## AgentLLMProviderErrors

*critical.* More than 10% of model API calls fail after the SDK's own retries (2 by default).

- `status` label: `rate_limit` (429) → check usage against the org's rate limits; reduce concurrency or
  request a limit increase. `server` (5xx/529 overloaded) → provider-side; check the status page.
  `auth` → API key rotated or revoked. `timeout` → `LLM_TIMEOUT_S` vs *Model call latency*.
- The agent returns a 503 with a friendly message for these; nothing to roll back unless a deploy
  changed the model id or request shape (`bad_request`).

## AgentTargetDown

*critical.* Prometheus can't scrape an agent instance. `docker compose ps` / `kubectl get pods`.
Traefik's health check takes the instance out of rotation within ~10s.

## AgentLatencyP95High

*warning.* p95 above 20s for 10 minutes. Split it: *Model call latency p95* (provider or effort level)
vs *Model calls per request* (more steps). An `EFFORT` increase in a release shows up as both.

## AgentCostBurnHigh

*warning.* Spend above $60/hour for 15 minutes (tune the threshold per environment in
`deploy/prometheus/rules/alerts.yml`).

- Traffic or price? *Requests / s* vs *Cost per request*.
- Price per request up → *Tokens / s by type*: output tokens (verbosity, effort), input tokens
  (longer history, more tool output), cache hit ratio dropping (something made the prompt prefix
  non-deterministic: timestamps in the system prompt, unsorted tools).
- Per-request guardrail `MAX_REQUEST_COST_USD` caps the worst single request; it does not cap volume.

## AgentUnpricedModel

*warning.* A model id without a price in `agent/pricing.py` served traffic (usually a refusal
fallback, or a release switching `MODEL`). Cost dashboards under-report until you add the price.

## AgentCanaryFailureRegression

*critical.* The canary fails more than 2 percentage points more often than stable. The controller
aborts on it; if the controller is not running (manual canary), run `make rollback`.

## AgentCanaryCostRegression

*warning.* Canary cost per request more than 30% above stable for 5 minutes. The canary gate
(+20%) normally catches this first.

---

## Releasing

A release is `releases/<version>.env` (prompt version, model, effort, guardrail limits) plus the
image. Locally:

```bash
make canary RELEASE=v1.4.1     # eval gate -> 10% -> 25% -> 50% -> promote, or automatic rollback
make status
```

What the controller checks at every step is in `canary/policy.yaml`; every decision is appended
to `deploy/state/events.jsonl` with the numbers it saw, and annotated on the Grafana dashboards.

On Kubernetes: run the **deploy** workflow with `action=ship, release=v1.4.1`. Argo Rollouts
runs the same gates (`deploy/k8s/analysis-template.yaml`); a failed metric aborts the rollout and
moves all traffic back to stable.

## Rolling back

| Situation | Local | Kubernetes |
|---|---|---|
| Canary misbehaving | `make rollback` (traffic off the canary first, then the container is reset) | `kubectl argo rollouts abort support-agent -n support-agent` |
| Bad release already promoted | `make rollback` (stable → previous release from `deploy/state/release.json`, without dropping requests) | deploy workflow `action=rollback` (`kubectl argo rollouts undo`) |
| Bad config only | edit `releases/<version>.env`, ship as a new version | edit `configmap.yaml`, ship (goes through the same canary) |

Rolling back is always the first move when a deploy is in the window. Debug afterwards, from the
traces of the failed requests, which stay in Phoenix (tail sampling keeps 100% of error traces).
