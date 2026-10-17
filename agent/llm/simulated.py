"""A deterministic stand-in for Claude, used for CI, load tests and drills.

It behaves like a tool-using model at the protocol level (tool_use blocks,
tool_result round trips, end_turn, token usage, latency) so the agent loop,
guardrails, metrics, traces, dashboards, alerts and canary analysis can all be
exercised without an API key or spend. It is not a language model: it routes on
keywords and writes templated answers.

Fault injection (per release, via SIM_* env vars) reproduces the failure modes
the observability stack exists to catch:
  SIM_LOOP_RATE         share of requests where the model re-issues the same
                        tool call forever (caught by the loop detector)
  SIM_ERROR_RATE        share of model calls that fail with a 5xx
  SIM_LATENCY_MULTIPLIER  slower model
  SIM_VERBOSITY         more output tokens -> cost regression

Token usage is estimated at ~4 characters per token and priced as the model
named in MODEL, so cost dashboards show plausible magnitudes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import uuid
from typing import Any

from agent.config import Settings
from agent.llm.base import LLMError, LLMResponse, ToolCall
from agent.pricing import Usage

ORDER_RE = re.compile(r"#?\b(\d{5})\b")
MATH_RE = re.compile(r"(\d+(?:\.\d+)?\s*[-+*/%]\s*\d+(?:\.\d+)?(?:\s*[-+*/%]\s*\d+(?:\.\d+)?)*)")
POLICY_WORDS = {
    "return": "return policy",
    "refund": "refund timing",
    "ship": "shipping times",
    "deliver": "shipping times",
    "warrant": "warranty",
    "defect": "warranty",
    "broken": "warranty",
    "cancel": "cancelling an order",
    "pay": "payment methods",
    "invoice": "payment methods",
    "price match": "price match",
    "cheaper": "price match",
}
HUMAN_WORDS = ("human", "person", "manager", "escalate", "complaint", "speak to")


def _est_tokens(obj: Any) -> int:
    if not isinstance(obj, str):
        obj = json.dumps(obj, default=str)
    return max(1, len(obj) // 4)


def _unit(seed: str) -> float:
    """Deterministic uniform [0, 1) from a string."""
    return int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _block_type(b: Any) -> str | None:
    return b.get("type") if isinstance(b, dict) else getattr(b, "type", None)


class SimulatedClient:
    provider = "simulated"

    def __init__(self, settings: Settings):
        self.model = settings.model
        self._s = settings

    def _plan(self, question: str) -> list[tuple[str, dict[str, Any]]]:
        q = question.lower()
        plan: list[tuple[str, dict[str, Any]]] = []
        for oid in dict.fromkeys(ORDER_RE.findall(question)):
            plan.append(("get_order", {"order_id": oid}))
        topics = list(dict.fromkeys(t for w, t in POLICY_WORDS.items() if w in q))
        if topics:
            plan.append(("search_kb", {"query": " ".join(topics[:2])}))
        m = MATH_RE.search(question)
        if m:
            plan.append(("calculate", {"expression": m.group(1).replace(" ", "")}))
        if any(w in q for w in HUMAN_WORDS):
            plan.append(("create_ticket", {"summary": question[:200], "priority": "normal"}))
        return plan

    async def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        request_id: str,
        step: int,
    ) -> LLMResponse:
        s = self._s
        rng = random.Random(f"{s.sim_seed}:{request_id}:{step}")
        question = next(m["content"] for m in messages if m["role"] == "user" and isinstance(m["content"], str))

        # Tool results already in the history.
        results: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "user" and isinstance(m["content"], list):
                results += [b for b in m["content"] if _block_type(b) == "tool_result"]
        calls_made = sum(
            1 for m in messages if m["role"] == "assistant" for b in m["content"] if _block_type(b) == "tool_use"
        )

        plan = self._plan(question)
        looping = plan and _unit(f"loop:{s.sim_seed}:{request_id}") < s.sim_loop_rate
        if looping:
            # The failure mode: re-verifying the same fact forever.
            next_call: tuple[str, dict[str, Any]] | None = plan[0]
        else:
            next_call = plan[calls_made] if calls_made < len(plan) else None

        if next_call:
            name, args = next_call
            preface = f"Let me check that with {name}."
            tool_id = f"toolu_sim_{uuid.uuid4().hex[:12]}"
            content = [
                {"type": "text", "text": preface},
                {"type": "tool_use", "id": tool_id, "name": name, "input": args},
            ]
            out_text = preface + json.dumps(args)
            stop = "tool_use"
            calls = [ToolCall(id=tool_id, name=name, input=args)]
            text = preface
        else:
            text = self._answer(question, results)
            content = [{"type": "text", "text": text}]
            out_text = text
            stop = "end_turn"
            calls = []

        input_tokens = _est_tokens(system) + _est_tokens(tools) + _est_tokens(messages)
        output_tokens = int(_est_tokens(out_text) * s.sim_verbosity) + 20
        # Prompt cache: the system+tools prefix is shared by every request, so it is
        # a cache read except when the 5-minute cache has gone cold (~10% of first steps).
        prefix = _est_tokens(system) + _est_tokens(tools)
        cold = step == 1 and rng.random() < 0.10
        cache_read = 0 if cold else prefix
        cache_write = prefix if cold else 0
        usage = Usage(input_tokens - cache_read - cache_write, output_tokens, cache_read, cache_write)

        latency_ms = (s.sim_base_latency_ms + s.sim_latency_per_output_token_ms * output_tokens) * (
            rng.lognormvariate(0, 0.35) * s.sim_latency_multiplier
        )
        await asyncio.sleep(latency_ms / 1000)

        if rng.random() < s.sim_error_rate:
            raise LLMError("simulated overloaded_error (529)", kind="server", retryable=True, status_code=529)

        return LLMResponse(
            content=content,
            text=text,
            tool_calls=calls,
            stop_reason=stop,
            usage=usage,
            requested_model=self.model,
            served_model=self.model,
            request_id=f"req_sim_{uuid.uuid4().hex[:16]}",
        )

    def _answer(self, question: str, results: list[dict[str, Any]]) -> str:
        if not results:
            return (
                "I can help with orders, returns, refunds, shipping, warranty, cancellations and payments. "
                "Could you share your order number or tell me a bit more about what you need?"
            )
        parts: list[str] = []
        for r in results:
            raw = r.get("content")
            if r.get("is_error"):
                parts.append(f"I couldn't complete one lookup ({raw}). Please double-check the order number.")
                continue
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                continue
            if "order_id" in data and "status" in data:
                items = ", ".join(i["name"] for i in data.get("items", []))
                parts.append(f"Order {data['order_id']} ({items}) is currently {data['status']}.")
            elif "results" in data:
                for doc in data["results"][:1]:
                    parts.append(f"{doc['title']}: {doc['text']}")
            elif "result" in data:
                parts.append(f"{data['expression']} = {data['result']}.")
            elif "ticket_id" in data:
                parts.append(f"I've opened ticket {data['ticket_id']}; a colleague will get back to you.")
        return " ".join(parts) if parts else "Sorry, I couldn't find anything relevant."
