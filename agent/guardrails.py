"""Runtime guardrails that stop a request before it burns money or time.

Each check returns a `Trip` naming the guardrail; the runner ends the request
with that outcome, records `agent_guardrail_trips_total{guardrail=...}`, and
marks the trace span as an error. These signals feed the loop and failure alerts.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Trip:
    guardrail: str  # loop_detected | max_steps | token_budget | cost_budget | timeout
    detail: str


def call_signature(name: str, args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return name + ":" + hashlib.sha1(canonical.encode()).hexdigest()[:12]


@dataclass
class LoopDetector:
    """Detects two loop shapes:

    * the same tool with the same arguments issued `max_identical` times;
    * a repeating cycle of calls (A,B,A,B / A,B,C,A,B,C) of period <= 3,
      seen twice in a row at the tail of the call sequence.
    Identical calls with *different* arguments (paging, retries with a fix) are fine.
    """

    max_identical: int = 3
    history: list[str] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)

    def observe(self, name: str, args: dict[str, Any]) -> Trip | None:
        sig = call_signature(name, args)
        self.history.append(sig)
        self.counts[sig] += 1
        if self.counts[sig] >= self.max_identical:
            return Trip("loop_detected", f"{name} called {self.counts[sig]}x with identical arguments")
        h = self.history
        for period in (2, 3):
            if len(h) >= 2 * period and h[-period:] == h[-2 * period : -period] and len(set(h[-period:])) > 1:
                return Trip("loop_detected", f"repeating cycle of {period} tool calls")
        return None


@dataclass
class Budget:
    max_steps: int
    max_tokens: int
    max_cost_usd: float
    timeout_s: float
    started: float = field(default_factory=time.monotonic)
    steps: int = 0
    tokens: int = 0
    cost_usd: float = 0.0

    def charge(self, tokens: int, cost_usd: float) -> Trip | None:
        self.tokens += tokens
        self.cost_usd += cost_usd
        if self.tokens > self.max_tokens:
            return Trip("token_budget", f"{self.tokens} tokens > {self.max_tokens}")
        if self.cost_usd > self.max_cost_usd:
            return Trip("cost_budget", f"${self.cost_usd:.4f} > ${self.max_cost_usd:.2f}")
        return None

    def next_step(self) -> Trip | None:
        if self.steps >= self.max_steps:
            return Trip("max_steps", f"reached {self.max_steps} steps without a final answer")
        if self.elapsed() > self.timeout_s:
            return Trip("timeout", f"{self.elapsed():.1f}s > {self.timeout_s:.0f}s")
        self.steps += 1
        return None

    def remaining_s(self) -> float:
        return max(0.0, self.timeout_s - self.elapsed())

    def elapsed(self) -> float:
        return time.monotonic() - self.started
