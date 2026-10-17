"""The agent loop: model -> tools -> model ... with guardrails and telemetry on
every step.

Outcomes (the `outcome` label on agent_requests_total):
  success         final answer produced
  loop_detected   repeated tool calls (guardrails.LoopDetector)
  max_steps       step budget exhausted without an answer
  token_budget    per-request token budget exceeded
  cost_budget     per-request USD budget exceeded
  timeout         wall-clock budget exceeded
  refusal         model declined (after any server-side fallback)
  provider_error  model API failed after the SDK's retries
  error           unexpected exception in the agent itself
Anything other than `success` counts against the availability SLO.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent import tools as toolbox
from agent.config import Settings
from agent.guardrails import Budget, LoopDetector, Trip
from agent.llm import LLMClient, LLMError
from agent.pricing import Usage, cost_usd, is_priced
from agent.telemetry import metrics as m
from agent.telemetry import tracing as t

log = logging.getLogger("agent")

PROMPTS = Path(__file__).parent / "prompts"

FALLBACK_ANSWERS = {
    "loop_detected": "Sorry, I got stuck looking this up. Please try again or ask for a human agent.",
    "max_steps": "Sorry, this is taking me longer than it should. Please try again or ask for a human agent.",
    "token_budget": "Sorry, I couldn't finish this request. Please try again or ask for a human agent.",
    "cost_budget": "Sorry, I couldn't finish this request. Please try again or ask for a human agent.",
    "timeout": "Sorry, this took too long. Please try again in a moment.",
    "refusal": "Sorry, I can't help with that request.",
    "provider_error": "Our assistant is temporarily unavailable. Please try again in a moment.",
    "error": "Something went wrong on our side. Please try again in a moment.",
}


@dataclass
class ToolTrace:
    name: str
    input: dict[str, Any]
    status: str
    duration_ms: float


@dataclass
class AgentResult:
    request_id: str
    trace_id: str
    outcome: str
    answer: str
    steps: int
    usage: Usage
    cost_usd: float
    duration_s: float
    model: str
    release: str
    track: str
    tool_calls: list[ToolTrace] = field(default_factory=list)
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "outcome": self.outcome,
            "answer": self.answer,
            "steps": self.steps,
            "tool_calls": [tc.__dict__ for tc in self.tool_calls],
            "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "cache_read_tokens": self.usage.cache_read_tokens,
                "cache_write_tokens": self.usage.cache_write_tokens,
            },
            "cost_usd": round(self.cost_usd, 6),
            "duration_s": round(self.duration_s, 3),
            "model": self.model,
            "release": self.release,
            "track": self.track,
            "detail": self.detail,
        }


class Agent:
    def __init__(self, settings: Settings, llm: LLMClient):
        self.s = settings
        self.llm = llm
        self.system = (PROMPTS / f"{settings.prompt_version}.txt").read_text()
        self.tools = toolbox.tool_specs()
        self.labels = {"release": settings.release, "track": settings.deployment_track}
        m.BUILD_INFO.labels(
            **self.labels, provider=llm.provider, model=llm.model, prompt_version=settings.prompt_version
        ).set(1)

    async def run(
        self, question: str, *, session_id: str | None = None, user_id: str | None = None
    ) -> AgentResult:
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        m.INFLIGHT.labels(**self.labels).inc()
        with t.tracer().start_as_current_span("agent.run") as span:
            t.mark_agent_span(span, request_id=request_id, session_id=session_id, user_id=user_id)
            t.set_io(span, input=question)
            trace_id = format(span.get_span_context().trace_id, "032x")
            state = _RunState(budget=self._budget(), loops=LoopDetector(self.s.max_identical_tool_calls))
            try:
                outcome, answer, detail = await asyncio.wait_for(
                    self._loop(question, request_id, state), timeout=self.s.request_timeout_s
                )
            except TimeoutError:
                outcome, answer, detail = "timeout", FALLBACK_ANSWERS["timeout"], "request deadline exceeded"
                m.GUARDRAIL_TRIPS.labels(**self.labels, guardrail="timeout").inc()
            except Exception as e:  # the request must always get an answer and a metric
                log.exception("agent error request_id=%s", request_id)
                outcome, answer, detail = "error", FALLBACK_ANSWERS["error"], f"{type(e).__name__}: {e}"

            duration = time.perf_counter() - started
            span.set_attribute("agent.outcome", outcome)
            span.set_attribute("agent.steps", state.budget.steps)
            span.set_attribute("agent.cost_usd", state.cost)
            span.set_attribute("llm.token_count.total", state.usage.total)
            span.set_attribute("agent.tool_call_count", len(state.tool_calls))
            t.set_io(span, output=answer)
            if outcome != "success":
                t.fail(span, outcome, detail or "")

        m.INFLIGHT.labels(**self.labels).dec()
        m.REQUESTS.labels(**self.labels, outcome=outcome).inc()
        m.REQUEST_LATENCY.labels(**self.labels, outcome=outcome).observe(duration)
        m.STEPS.labels(**self.labels).observe(state.budget.steps)
        m.REQUEST_COST.labels(**self.labels).observe(state.cost)
        result = AgentResult(
            request_id=request_id,
            trace_id=trace_id,
            outcome=outcome,
            answer=answer,
            steps=state.budget.steps,
            usage=state.usage,
            cost_usd=state.cost,
            duration_s=duration,
            model=state.served_model or self.llm.model,
            release=self.s.release,
            track=self.s.deployment_track,
            tool_calls=state.tool_calls,
            detail=detail,
        )
        log.info(
            json.dumps(
                {
                    "event": "agent_request",
                    **{k: v for k, v in result.to_dict().items() if k not in ("answer", "tool_calls")},
                    "tools": [tc.name for tc in state.tool_calls],
                }
            )
        )
        return result

    def _budget(self) -> Budget:
        return Budget(
            max_steps=self.s.max_steps,
            max_tokens=self.s.max_request_tokens,
            max_cost_usd=self.s.max_request_cost_usd,
            timeout_s=self.s.request_timeout_s,
        )

    def _trip(self, trip: Trip) -> tuple[str, str, str]:
        m.GUARDRAIL_TRIPS.labels(**self.labels, guardrail=trip.guardrail).inc()
        span = t.trace.get_current_span()
        span.add_event("guardrail.tripped", {"guardrail": trip.guardrail, "detail": trip.detail})
        log.warning("guardrail tripped: %s (%s)", trip.guardrail, trip.detail)
        return trip.guardrail, FALLBACK_ANSWERS[trip.guardrail], trip.detail

    async def _loop(self, question: str, request_id: str, st: _RunState) -> tuple[str, str, str | None]:
        messages: list[dict[str, Any]] = [{"role": "user", "content": question}]
        while True:
            if trip := st.budget.next_step():
                return self._trip(trip)
            step = st.budget.steps
            try:
                resp = await self._call_llm(messages, request_id, step, st)
            except LLMError as e:
                return "provider_error", FALLBACK_ANSWERS["provider_error"], f"{e.kind}: {e}"

            if trip := st.budget.charge(resp.usage.total, st.last_cost):
                return self._trip(trip)

            if resp.stop_reason == "refusal":
                m.REFUSALS.labels(**self.labels, category=resp.refusal_category or "none").inc()
                return "refusal", FALLBACK_ANSWERS["refusal"], resp.refusal_category

            if not resp.tool_calls:
                if resp.stop_reason == "max_tokens" and not resp.text:
                    return "error", FALLBACK_ANSWERS["error"], "max_tokens with empty answer"
                return "success", resp.text, None

            # Append the assistant turn verbatim, then one user turn with every result.
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for call in resp.tool_calls:
                if trip := st.loops.observe(call.name, call.input):
                    return self._trip(trip)
                results.append(self._run_tool(call, st))
            messages.append({"role": "user", "content": results})

    async def _call_llm(self, messages: list[dict[str, Any]], request_id: str, step: int, st: _RunState):
        labels = {**self.labels, "provider": self.llm.provider, "model": self.llm.model}
        with t.tracer().start_as_current_span("llm.call") as span:
            t.mark_llm_span(
                span, provider=self.llm.provider, model=self.llm.model, step=step, messages=messages
            )
            started = time.perf_counter()
            try:
                resp = await self.llm.complete(
                    system=self.system, tools=self.tools, messages=messages, request_id=request_id, step=step
                )
            except LLMError as e:
                m.LLM_LATENCY.labels(**labels).observe(time.perf_counter() - started)
                m.LLM_CALLS.labels(**labels, status=e.kind).inc()
                t.fail(span, f"llm.{e.kind}", str(e))
                if e.status_code:
                    span.set_attribute("http.response.status_code", e.status_code)
                raise
            m.LLM_LATENCY.labels(**labels).observe(time.perf_counter() - started)
            m.LLM_CALLS.labels(**labels, status="ok").inc()

            model = resp.served_model
            cost = cost_usd(model, resp.usage)
            if not is_priced(model):
                m.UNPRICED.labels(model=model).inc()
            u = resp.usage
            for kind, n in (
                ("input", u.input_tokens),
                ("output", u.output_tokens),
                ("cache_read", u.cache_read_tokens),
                ("cache_write", u.cache_write_tokens),
            ):
                if n:
                    m.TOKENS.labels(**self.labels, model=model, type=kind).inc(n)
            m.COST.labels(**self.labels, model=model).inc(cost)
            if resp.fallback_used:
                m.FALLBACKS.labels(**self.labels, model=model).inc()
            t.record_llm_result(span, resp, cost)

            st.usage = st.usage + u
            st.cost += cost
            st.last_cost = cost
            st.served_model = model
            return resp

    def _run_tool(self, call: Any, st: _RunState) -> dict[str, Any]:
        with t.tracer().start_as_current_span(f"tool.{call.name}") as span:
            t.mark_tool_span(span, name=call.name, args=call.input, call_id=call.id)
            started = time.perf_counter()
            try:
                output = toolbox.run_tool(call.name, call.input)
                status, content, is_error = "ok", json.dumps(output, ensure_ascii=False), False
            except toolbox.ToolError as e:
                status, content, is_error = "error", f"Error: {e}", True
                t.fail(span, "tool_error", str(e))
            elapsed = time.perf_counter() - started
            t.set_io(span, output=content)
            m.TOOL_CALLS.labels(**self.labels, tool=call.name, status=status).inc()
            m.TOOL_LATENCY.labels(**self.labels, tool=call.name).observe(elapsed)
            st.tool_calls.append(ToolTrace(call.name, call.input, status, round(elapsed * 1000, 2)))
            result: dict[str, Any] = {"type": "tool_result", "tool_use_id": call.id, "content": content}
            if is_error:
                result["is_error"] = True
            return result


@dataclass
class _RunState:
    budget: Budget
    loops: LoopDetector
    usage: Usage = field(default_factory=Usage)
    cost: float = 0.0
    last_cost: float = 0.0
    served_model: str | None = None
    tool_calls: list[ToolTrace] = field(default_factory=list)
