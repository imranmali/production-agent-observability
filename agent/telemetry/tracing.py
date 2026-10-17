"""OpenTelemetry tracing for the agent.

One trace per request:

    agent.run                    AGENT   (root; request in/out, outcome, totals)
    ├── llm.call  step=1         LLM     (model, tokens, cost, stop reason)
    ├── tool.get_order           TOOL    (arguments, result, status)
    ├── llm.call  step=2         LLM
    └── ...

Spans carry three attribute vocabularies at once so the same trace renders
natively in every backend without per-vendor code:

* OpenInference (`openinference.span.kind`, `llm.token_count.*`, `input.value`)
  -> Arize Phoenix and Arize AX
* OpenTelemetry GenAI semconv (`gen_ai.*`) -> LangSmith, Datadog, Grafana, any OTLP backend
* LangSmith hints (`langsmith.span.kind`, `langsmith.metadata.*`)

Spans leave the process over OTLP/HTTP, normally to the OpenTelemetry
Collector, which fans out to Phoenix / LangSmith / Arize (deploy/otel-collector).
For a setup without a collector, LANGSMITH_API_KEY and PHOENIX_COLLECTOR_ENDPOINT
add direct exporters.
"""

from __future__ import annotations

import json
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Span, Status, StatusCode

from agent.config import Settings

_settings: Settings | None = None


def setup_tracing(settings: Settings) -> TracerProvider:
    global _settings
    _settings = settings
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.release,
            "deployment.environment": settings.environment,
            "deployment.track": settings.deployment_track,
        }
    )
    provider = TracerProvider(resource=resource)
    if settings.otel_exporter_otlp_endpoint:
        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    if settings.phoenix_collector_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.phoenix_collector_endpoint))
        )
    if settings.langsmith_api_key:
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=settings.langsmith_otlp_endpoint,
                    headers={
                        "x-api-key": settings.langsmith_api_key,
                        "Langsmith-Project": settings.langsmith_project,
                    },
                )
            )
        )
    if settings.otel_console:
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    trace.set_tracer_provider(provider)
    return provider


def tracer() -> trace.Tracer:
    return trace.get_tracer("support-agent")


def _clip(value: Any) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    limit = _settings.trace_max_attr_chars if _settings else 4000
    return s if len(s) <= limit else s[:limit] + "…[truncated]"


def capture_content() -> bool:
    return _settings.trace_capture_content if _settings else True


def set_io(span: Span, *, input: Any = None, output: Any = None) -> None:
    """Prompt/response payloads; dropped entirely when TRACE_CAPTURE_CONTENT=false."""
    if not capture_content():
        return
    if input is not None:
        span.set_attribute("input.value", _clip(input))
        span.set_attribute("input.mime_type", "text/plain" if isinstance(input, str) else "application/json")
    if output is not None:
        span.set_attribute("output.value", _clip(output))
        span.set_attribute(
            "output.mime_type", "text/plain" if isinstance(output, str) else "application/json"
        )


def mark_agent_span(span: Span, *, request_id: str, session_id: str | None, user_id: str | None) -> None:
    s = _settings
    span.set_attribute("openinference.span.kind", "AGENT")
    span.set_attribute("langsmith.span.kind", "chain")
    span.set_attribute("gen_ai.operation.name", "invoke_agent")
    span.set_attribute("gen_ai.agent.name", s.service_name if s else "support-agent")
    span.set_attribute("agent.request_id", request_id)
    if session_id:
        span.set_attribute("session.id", session_id)
        span.set_attribute("gen_ai.conversation.id", session_id)
        span.set_attribute("langsmith.metadata.session_id", session_id)
    if user_id:
        span.set_attribute("user.id", user_id)
    if s:
        meta = {"release": s.release, "track": s.deployment_track, "prompt_version": s.prompt_version}
        span.set_attribute("metadata", json.dumps(meta))
        for k, v in meta.items():
            span.set_attribute(f"langsmith.metadata.{k}", v)
        span.set_attribute("tag.tags", [s.release, s.deployment_track])


def mark_llm_span(
    span: Span,
    *,
    provider: str,
    model: str,
    step: int,
    messages: list[dict[str, Any]] | None,
) -> None:
    span.set_attribute("openinference.span.kind", "LLM")
    span.set_attribute("langsmith.span.kind", "llm")
    span.set_attribute("gen_ai.operation.name", "chat")
    span.set_attribute("gen_ai.system", "anthropic")
    span.set_attribute("gen_ai.provider.name", "anthropic")
    span.set_attribute("gen_ai.request.model", model)
    span.set_attribute("llm.model_name", model)
    span.set_attribute("llm.provider", "anthropic")
    span.set_attribute("llm.system", "anthropic")
    span.set_attribute("agent.provider_impl", provider)
    span.set_attribute("agent.step", step)
    if messages is not None and capture_content():
        last = messages[-1]
        span.set_attribute("input.value", _clip(_render(last.get("content"))))
        span.set_attribute("llm.input_messages.0.message.role", last.get("role", "user"))
        span.set_attribute("llm.input_messages.0.message.content", _clip(_render(last.get("content"))))


def record_llm_result(span: Span, resp: Any, cost_usd: float) -> None:
    u = resp.usage
    prompt_tokens = u.input_tokens + u.cache_read_tokens + u.cache_write_tokens
    span.set_attribute("gen_ai.response.model", resp.served_model)
    span.set_attribute("gen_ai.response.finish_reasons", [resp.stop_reason])
    if resp.request_id:
        span.set_attribute("gen_ai.response.id", resp.request_id)
    span.set_attribute("gen_ai.usage.input_tokens", prompt_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", u.output_tokens)
    span.set_attribute("gen_ai.usage.cache_read.input_tokens", u.cache_read_tokens)
    span.set_attribute("gen_ai.usage.cache_creation.input_tokens", u.cache_write_tokens)
    span.set_attribute("llm.token_count.prompt", prompt_tokens)
    span.set_attribute("llm.token_count.completion", u.output_tokens)
    span.set_attribute("llm.token_count.total", prompt_tokens + u.output_tokens)
    span.set_attribute("llm.token_count.prompt_details.cache_read", u.cache_read_tokens)
    span.set_attribute("llm.token_count.prompt_details.cache_write", u.cache_write_tokens)
    span.set_attribute("llm.cost.total", cost_usd)
    span.set_attribute("agent.cost_usd", cost_usd)
    if resp.fallback_used:
        span.set_attribute("agent.refusal_fallback", True)
    if resp.refusal_category:
        span.set_attribute("agent.refusal_category", resp.refusal_category)
    if capture_content():
        out = resp.text or json.dumps([{"tool": c.name, "input": c.input} for c in resp.tool_calls])
        span.set_attribute("output.value", _clip(out))
        span.set_attribute("llm.output_messages.0.message.role", "assistant")
        span.set_attribute("llm.output_messages.0.message.content", _clip(out))
        for i, c in enumerate(resp.tool_calls):
            prefix = f"llm.output_messages.0.message.tool_calls.{i}.tool_call"
            span.set_attribute(f"{prefix}.function.name", c.name)
            span.set_attribute(f"{prefix}.function.arguments", _clip(c.input))


def mark_tool_span(span: Span, *, name: str, args: dict[str, Any], call_id: str) -> None:
    span.set_attribute("openinference.span.kind", "TOOL")
    span.set_attribute("langsmith.span.kind", "tool")
    span.set_attribute("gen_ai.operation.name", "execute_tool")
    span.set_attribute("gen_ai.tool.name", name)
    span.set_attribute("gen_ai.tool.call.id", call_id)
    span.set_attribute("tool.name", name)
    if capture_content():
        span.set_attribute("tool.parameters", _clip(args))
        span.set_attribute("input.value", _clip(args))


def fail(span: Span, kind: str, detail: str) -> None:
    span.set_status(Status(StatusCode.ERROR, f"{kind}: {detail}"))
    span.set_attribute("error.type", kind)


def _render(content: Any) -> Any:
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        d = b if isinstance(b, dict) else b.model_dump(exclude_none=True)
        out.append(d)
    return out
