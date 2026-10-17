# ADR 0005: A simulated model provider for CI, load tests and drills

**Status:** accepted

## Context

The observability stack has to be exercised end to end: in CI on every push, under load, and in
release drills that deliberately ship a bad release. Doing that against the real API costs money,
needs a key in CI, is non-deterministic, and can't produce a specific failure (a loop rate of exactly
15%) on demand.

## Decision

`LLM_PROVIDER=simulated` swaps in `agent/llm/simulated.py`: it speaks the same protocol as Claude at
the level the agent sees (tool_use blocks, tool_result round trips, stop reasons, token usage with
prompt-cache reads, latency) and routes on keywords to produce plausible tool plans and templated
answers. Fault injection per release (`SIM_LOOP_RATE`, `SIM_ERROR_RATE`, `SIM_LATENCY_MULTIPLIER`,
`SIM_VERBOSITY`) reproduces the failure modes the gates must catch. Token usage is priced as the model
named in `MODEL`, so dollar figures have realistic magnitudes.

`LLM_PROVIDER=anthropic` uses Claude through the official SDK; nothing else in the stack changes.

## Consequences

- CI runs the full stack (gateway, both tracks, collector, Phoenix, Prometheus) with no secrets.
- Every number in `docs/results/` is from the simulator. They demonstrate that the *pipeline* (metrics →
  analysis → rollback, alerts → notifications) works and how fast; they say nothing about Claude's quality
  on this task. The README says so wherever it shows them.
- The bad release `v1.5.0` pairs a plausible prompt regression (prompt v2: "re-check whenever not completely
  certain") with fault injection that models its effect. With the real provider the fault settings are
  ignored and the prompt itself is what gets tested.
