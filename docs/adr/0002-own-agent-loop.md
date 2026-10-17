# ADR 0002: The agent owns its loop

**Status:** accepted

## Context

The Anthropic SDK's tool runner drives the request → tool → request loop for you. It has per-turn
hooks, but the loop's termination is the SDK's.

## Decision

`agent/runner.py` runs the loop itself. Every iteration goes through, in order: step budget and
deadline check → model call (one `llm.call` span, token/cost metrics) → token and cost budget
check → refusal handling → for each tool call: loop detector, then the tool (one `tool.*` span).

## Consequences

- Guardrails can stop a request *between* any two steps, and each stop is a distinct, labelled
  outcome (`loop_detected`, `max_steps`, `token_budget`, `cost_budget`, `timeout`, `refusal`,
  `provider_error`, `error`), which is what the alerts and the canary analysis count.
- The history is append-only: assistant turns are appended verbatim, so thinking blocks stay valid
  and the prompt-cache prefix stays stable.
- More code to own than the tool runner (~250 lines). Worth it here because the loop *is* the
  thing being observed and governed.
