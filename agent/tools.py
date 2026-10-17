"""The agent's tools: a customer-support toolset for a small online store.

Each tool is a plain function plus a strict JSON schema. Tool failures are
returned to the model as `is_error` results rather than raised, so the model
can recover; the runner counts them in `agent_tool_calls_total{status="error"}`.
"""

from __future__ import annotations

import ast
import json
import operator
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATA = Path(__file__).parent / "data"
KB: list[dict[str, str]] = json.loads((DATA / "kb.json").read_text())
ORDERS: dict[str, dict[str, Any]] = json.loads((DATA / "orders.json").read_text())


class ToolError(Exception):
    pass


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def search_kb(query: str, top_k: int = 2) -> dict[str, Any]:
    """Keyword-overlap search over the policy knowledge base."""
    q = set(_tokens(query))
    if not q:
        raise ToolError("query is empty")
    scored = []
    for doc in KB:
        words = _tokens(doc["title"] + " " + doc["text"])
        score = sum(1 for w in words if w in q) + 3 * sum(1 for w in _tokens(doc["title"]) if w in q)
        if score:
            scored.append((score, doc))
    scored.sort(key=lambda s: -s[0])
    return {"results": [{"id": d["id"], "title": d["title"], "text": d["text"]} for _, d in scored[:top_k]]}


def get_order(order_id: str) -> dict[str, Any]:
    oid = order_id.strip().lstrip("#")
    order = ORDERS.get(oid)
    if order is None:
        raise ToolError(f"order {oid} not found")
    return {"order_id": oid, **order}


_OPS: dict[type, Callable[..., float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.Mod: operator.mod,
}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        if isinstance(node.op, ast.Pow) and abs(_eval(node.right)) > 10:
            raise ToolError("exponent too large")
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ToolError("unsupported expression")


def calculate(expression: str) -> dict[str, Any]:
    """Safe arithmetic (no names, calls or attribute access)."""
    if len(expression) > 200:
        raise ToolError("expression too long")
    try:
        value = _eval(ast.parse(expression, mode="eval").body)
    except (SyntaxError, ZeroDivisionError) as e:
        raise ToolError(f"cannot evaluate: {e}") from e
    return {"expression": expression, "result": round(value, 4)}


TICKETS: list[dict[str, Any]] = []


def create_ticket(summary: str, priority: str) -> dict[str, Any]:
    if priority not in {"low", "normal", "high"}:
        raise ToolError("priority must be low, normal or high")
    ticket = {"ticket_id": f"T-{uuid.uuid4().hex[:8]}", "summary": summary[:500], "priority": priority}
    TICKETS.append(ticket)
    return {**ticket, "status": "open"}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., dict[str, Any]]

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "strict": True,
        }


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "search_kb",
            "Search the store's policy knowledge base (returns, refunds, shipping, warranty, "
            "cancellation, payment, price match, escalation). Use it before answering any policy question.",
            _schema({"query": {"type": "string", "description": "Keywords to search for"}}, ["query"]),
            search_kb,
        ),
        Tool(
            "get_order",
            "Look up an order by its 5-digit order number. Returns status, items, dates and totals.",
            _schema({"order_id": {"type": "string", "description": "Order number, e.g. 10231"}}, ["order_id"]),
            get_order,
        ),
        Tool(
            "calculate",
            "Evaluate an arithmetic expression (+ - * / % **). Use it for refund amounts and totals.",
            _schema({"expression": {"type": "string"}}, ["expression"]),
            calculate,
        ),
        Tool(
            "create_ticket",
            "Escalate to a human agent by opening a support ticket. Only use when the customer asks for a "
            "human, or the request cannot be resolved with the other tools.",
            _schema(
                {
                    "summary": {"type": "string"},
                    "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                },
                ["summary", "priority"],
            ),
            create_ticket,
        ),
    ]
}


def tool_specs() -> list[dict[str, Any]]:
    # Deterministic order keeps the tools block byte-stable for prompt caching.
    return [TOOLS[name].spec() for name in sorted(TOOLS)]


def run_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(f"unknown tool {name}")
    try:
        return tool.fn(**args)
    except TypeError as e:
        raise ToolError(f"bad arguments: {e}") from e
