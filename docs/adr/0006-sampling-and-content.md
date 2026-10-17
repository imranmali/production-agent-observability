# ADR 0006: Tail sampling, and what content leaves the process

**Status:** accepted

## Context

Agent traces are large (prompts, tool inputs and outputs) and contain customer messages and order data.
At production volume, keeping every trace in a hosted LLM-observability product is expensive, and shipping
customer text to a third party needs approval.

## Decision

- **Tail sampling in the collector:** keep 100% of traces with an error status (every guardrail trip,
  provider error, refusal), 100% of traces slower than 15s, and `TRACE_SAMPLE_PERCENT` of the rest
  (100 locally). `decision_wait` (100s) is longer than the request timeout (90s), so a trace's root span
  arrives before its sampling decision.
- **Metrics are never sampled.** Rates, latency and cost come from Prometheus counters and histograms,
  which see every request. Sampling only affects which traces you can open.
- **Content capture is a switch:** `TRACE_CAPTURE_CONTENT=false` (the Kubernetes default) drops prompts,
  responses and tool arguments/results from spans and keeps structure, timing, token counts, cost,
  outcome and tool names. Long attributes are truncated at `TRACE_MAX_ATTR_CHARS`.

## Consequences

- Every failed request can be debugged from its trace, even at 5% baseline sampling.
- Sampled-out successful traces can't be used to build eval sets; turn sampling up temporarily, or log
  eval candidates separately.
- The collector holds traces in memory for 100s; `num_traces` and the memory limiter bound that.
