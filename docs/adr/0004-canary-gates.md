# ADR 0004: Canary gates on agent-level signals, with an eval gate before traffic

**Status:** accepted

## Context

Most agent regressions come from prompts, model ids, effort levels and tool schemas, not from code
that crashes. HTTP error rate and CPU, the usual canary signals, barely move when a prompt starts
making the model loop or doubles its output length.

## Decision

A release moves through two kinds of gates (`canary/policy.yaml`):

1. **Eval gate, zero customer traffic.** The golden set (`evals/golden.jsonl`) runs against the canary
   only (`X-Canary: always` locally, the canary Service on Kubernetes). It checks behaviour: success,
   expected tools called, key fact in the answer, step count. Pass rate must be ≥ 95% **and** no golden case may trip a guardrail (loop, step,
   token, cost or time budget). The second rule was added after the first drill, where a looping
   release passed at exactly 95%.
2. **Traffic steps 10% → 25% → 50%**, each analysed against stable over the step's window:

| Gate | Limit |
|---|---|
| failure ratio | ≤ min(5%, stable + 2pp), but never stricter than 2% |
| loop ratio | ≤ 1% |
| p95 latency | ≤ max(1.25× stable, stable + 0.5s) |
| cost per request | ≤ 1.2× stable |
| critical alerts for this canary (its release, raised after it started) | none firing |
| canary requests in window | ≥ 25, otherwise *inconclusive* (step extended up to twice, then rolled back) |

Any failure rolls back immediately: traffic weight to 0 first, cleanup second.

The same policy runs in two places: `canary/controller.py` for a single docker host, and an Argo Rollouts
`AnalysisTemplate` on Kubernetes. `tests/test_policy_sync.py` fails if their thresholds drift apart.

## Consequences

- Cost per request is a release gate, not just a dashboard: a 2× verbosity regression is caught at 10%
  traffic.
- Comparing against stable over the same window cancels shared noise (provider slowness hits both).
- With little traffic, percentile comparisons are noisy. The minimum-request rule and the latency slack
  keep a good release from failing on noise; at very low traffic, step durations need to be longer.
- Inconclusive defaults to rollback after two extensions. Conservative by design: shipping blind is worse
  than shipping late.

## Amendments after the 2026-10-17 drill

See `docs/results/drill-2026-10-17.md`. Zero-guardrail-trip rule in the eval gate; alert check scoped to
the canary's own release and start time (a still-firing alert from a previous attempt had failed the next
canary); canary alerts routed to chat instead of paging, because the controller rolls back before an
alert's `for:` window ends and the page arrived 93s after the rollback.
