# ADR 0001: OpenTelemetry and a collector, not vendor SDKs

**Status:** accepted

## Context

The brief asks for LangSmith or Arize tracing. Both ship their own SDKs (`langsmith.traceable`,
Arize's OpenInference instrumentors plus `arize-otel`), and both accept OpenTelemetry over OTLP.
Teams switch LLM-observability vendors often, and frequently run two at once during an evaluation.

## Decision

The agent emits plain OpenTelemetry spans over OTLP/HTTP to an OpenTelemetry Collector. The
collector decides where traces go: self-hosted Arize Phoenix always, plus LangSmith and/or Arize AX
through config overlays (`deploy/otel-collector/overlay.*.yaml`). The agent carries no vendor SDK.

Each span carries three attribute vocabularies at once (`agent/telemetry/tracing.py`):
OpenInference (`openinference.span.kind`, `llm.token_count.*`, `input.value`) for Phoenix and Arize,
the OpenTelemetry GenAI conventions (`gen_ai.*`) for LangSmith and generic backends, and LangSmith
hints (`langsmith.span.kind`, `langsmith.metadata.*`).

The Anthropic SDK's own OpenTelemetry spans are turned off (`ANTHROPIC_SDK_TRACING=false`): the agent
writes its own `llm.call` span for every provider, simulated included, and two spans carrying
`gen_ai.usage.*` for the same call would double the token counts in LangSmith.

## Consequences

- Adding or removing a vendor is a collector config change; no agent redeploy, no new dependency.
- Tail sampling, redaction and batching live in one place for all vendors.
- The collector is one more component to run and monitor (it is scraped by Prometheus at `:8888`).
- Three vocabularies mean more attributes per span. Cheap compared with an LLM call.
- Turning SDK spans off loses the SDK-to-API `traceparent` link. Setting `ANTHROPIC_SDK_TRACING=true`
  restores it, at the cost of duplicated usage attributes.
- What was verified here: Phoenix receives and renders the traces (AGENT / LLM / TOOL kinds).
  The LangSmith and Arize overlays are configuration only; they were not run against live accounts.
