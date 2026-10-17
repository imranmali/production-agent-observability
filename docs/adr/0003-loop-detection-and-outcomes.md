# ADR 0003: How loops are detected, and why every non-success counts against the SLO

**Status:** accepted

## Context

The characteristic production failure of a tool-using agent is not a crash but a loop: the model
re-verifies the same fact, or alternates between two tools, until something external stops it.
HTTP status codes don't show it (the request eventually returns 200), latency shows it late,
and cost shows it at the end of the month.

## Decision

Two detectors (`agent/guardrails.py`), checked before each tool call runs:

1. **Identical call:** the same tool with the same canonicalised arguments `MAX_IDENTICAL_TOOL_CALLS`
   times (default 3). Arguments are hashed with sorted keys, so key order can't hide a repeat. The same
   tool with different arguments (looking up several orders) is not a loop.
2. **Cycle:** the tail of the call sequence repeats with period 2 or 3 (A,B,A,B / A,B,C,A,B,C).

Plus hard budgets that bound what a loop the detectors miss can cost: `MAX_STEPS`, `MAX_REQUEST_TOKENS`,
`MAX_REQUEST_COST_USD`, `REQUEST_TIMEOUT_S`.

A tripped guardrail ends the request with a fallback answer, increments
`agent_guardrail_trips_total{guardrail}`, adds a `guardrail.tripped` event to the root span and marks
it as an error (so tail sampling keeps the trace).

The availability SLO counts **every outcome other than `success`** as an error, guardrail trips
included. A request the agent could not answer is a failure from the customer's side even if the
guardrail worked as designed.

## Consequences

- Loops become a first-class, alertable rate (`agent:loop_ratio:rate5m`): one loop is an `info`
  notification, more than 2% of a track's traffic pages.
- A release that makes the model loop fails canary analysis on the loop ratio before it fails on
  latency or cost.
- The detector can't see loops that change arguments every time (paging forever). The step and cost
  budgets bound those.
