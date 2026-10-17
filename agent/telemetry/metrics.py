"""Prometheus metrics.

Every series carries `release` and `track` (stable | canary) so the canary
analysis and the dashboards can compare the two side by side. Label values are
bounded: outcomes, guardrails, tool names and model ids are closed sets.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

BASE = ("release", "track")

# Agent requests take seconds, not milliseconds.
REQUEST_BUCKETS = (0.25, 0.5, 0.75, 1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5, 10, 12.5, 15, 20, 30, 45, 60, 90, 120)
LLM_BUCKETS = (0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 7.5, 10, 15, 20, 30, 60)
TOOL_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)
COST_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1)

BUILD_INFO = Gauge(
    "agent_build_info",
    "Static info about the running release",
    [*BASE, "provider", "model", "prompt_version"],
)
REQUESTS = Counter("agent_requests_total", "Agent requests by final outcome", [*BASE, "outcome"])
REQUEST_LATENCY = Histogram(
    "agent_request_duration_seconds",
    "End-to-end agent request latency",
    [*BASE, "outcome"],
    buckets=REQUEST_BUCKETS,
)
INFLIGHT = Gauge("agent_inflight_requests", "Requests currently being processed", BASE)
STEPS = Histogram(
    "agent_steps_per_request",
    "Model calls per request",
    BASE,
    buckets=(1, 2, 3, 4, 5, 6, 8, 10, 12, 16),
)
REQUEST_COST = Histogram("agent_request_cost_usd", "Cost of one agent request", BASE, buckets=COST_BUCKETS)

LLM_CALLS = Counter("agent_llm_requests_total", "Model API calls", [*BASE, "provider", "model", "status"])
LLM_LATENCY = Histogram(
    "agent_llm_latency_seconds", "Model API call latency", [*BASE, "provider", "model"], buckets=LLM_BUCKETS
)
TOKENS = Counter("agent_llm_tokens_total", "Tokens by type", [*BASE, "model", "type"])
COST = Counter("agent_llm_cost_usd_total", "Model spend in USD", [*BASE, "model"])
UNPRICED = Counter("agent_unpriced_model_calls_total", "Calls to a model missing from the price table", ["model"])
REFUSALS = Counter("agent_llm_refusals_total", "Model refusals", [*BASE, "category"])
FALLBACKS = Counter("agent_llm_fallbacks_total", "Requests served by a refusal-fallback model", [*BASE, "model"])

TOOL_CALLS = Counter("agent_tool_calls_total", "Tool invocations", [*BASE, "tool", "status"])
TOOL_LATENCY = Histogram("agent_tool_latency_seconds", "Tool latency", [*BASE, "tool"], buckets=TOOL_BUCKETS)

GUARDRAIL_TRIPS = Counter(
    "agent_guardrail_trips_total",
    "Requests stopped by a guardrail (loop_detected, max_steps, token_budget, cost_budget, timeout)",
    [*BASE, "guardrail"],
)
