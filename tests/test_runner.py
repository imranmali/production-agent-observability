"""End-to-end agent loop against the simulated provider, including the failure
modes the guardrails and metrics exist for."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from agent.config import Settings
from agent.llm.simulated import SimulatedClient
from agent.runner import Agent
from agent.telemetry import tracing

EXPORTER = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
trace.set_tracer_provider(_provider)


def make_agent(**overrides) -> Agent:
    s = Settings(_env_file=None, sim_base_latency_ms=1, sim_latency_per_output_token_ms=0, **overrides)
    tracing._settings = s
    return Agent(s, SimulatedClient(s))


def metric(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture(autouse=True)
def _clear_spans():
    EXPORTER.clear()


async def test_success_uses_tools_and_records_everything():
    agent = make_agent(release="t-ok")
    before = metric("agent_requests_total", release="t-ok", track="dev", outcome="success")
    r = await agent.run("Can I return order 10231?")
    assert r.outcome == "success"
    assert [tc.name for tc in r.tool_calls] == ["get_order", "search_kb"]
    assert r.steps == 3 and r.cost_usd > 0
    assert metric("agent_requests_total", release="t-ok", track="dev", outcome="success") == before + 1

    spans = {s.name: s for s in EXPORTER.get_finished_spans()}
    root = spans["agent.run"]
    assert root.attributes["openinference.span.kind"] == "AGENT"
    assert root.attributes["agent.outcome"] == "success"
    llm = next(s for s in EXPORTER.get_finished_spans() if s.name == "llm.call")
    assert llm.attributes["openinference.span.kind"] == "LLM"
    assert llm.attributes["gen_ai.usage.output_tokens"] > 0
    assert llm.parent.span_id == root.context.span_id
    assert spans["tool.get_order"].attributes["tool.name"] == "get_order"


async def test_loop_is_stopped_counted_and_marked_as_error():
    agent = make_agent(release="t-loop", sim_loop_rate=1.0, max_identical_tool_calls=3)
    r = await agent.run("Where is my order 10232?")
    assert r.outcome == "loop_detected"
    assert r.steps == 3  # third identical call is refused before it runs
    assert len(r.tool_calls) == 2
    assert metric("agent_guardrail_trips_total", release="t-loop", track="dev", guardrail="loop_detected") == 1
    root = next(s for s in EXPORTER.get_finished_spans() if s.name == "agent.run")
    assert root.status.status_code == trace.StatusCode.ERROR
    assert any(e.name == "guardrail.tripped" for e in root.events)


async def test_step_budget_stops_long_plans():
    agent = make_agent(release="t-steps", max_steps=2)
    r = await agent.run("I return order 10231. What refund do I get after the fee, 179 - 5.95?")
    assert r.outcome == "max_steps"


async def test_cost_budget():
    agent = make_agent(release="t-cost", max_request_cost_usd=0.0001)
    r = await agent.run("Where is my order 10232?")
    assert r.outcome == "cost_budget"


async def test_provider_error_is_an_outcome_not_an_exception():
    agent = make_agent(release="t-err", sim_error_rate=1.0)
    r = await agent.run("Where is my order 10232?")
    assert r.outcome == "provider_error"
    assert (
        metric(
            "agent_llm_requests_total",
            release="t-err",
            track="dev",
            provider="simulated",
            model="claude-opus-5-5",
            status="server",
        )
        == 1
    )


async def test_tool_error_is_returned_to_the_model():
    agent = make_agent(release="t-toolerr")
    r = await agent.run("Where is order 99999?")
    assert r.outcome == "success"
    assert r.tool_calls[0].status == "error"
    assert "99999" in r.answer


async def test_content_capture_can_be_turned_off():
    agent = make_agent(release="t-nocontent", trace_capture_content=False)
    await agent.run("Where is my order 10232?")
    for span in EXPORTER.get_finished_spans():
        assert "input.value" not in span.attributes
        assert "output.value" not in span.attributes
