"""Provider-neutral view of one model call, so the agent loop, guardrails and
telemetry don't care whether the model is Claude or the simulator."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.pricing import Usage


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    # Content blocks to append to the history verbatim (append-only history keeps
    # thinking blocks valid and the prompt cache warm).
    content: list[Any]
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    usage: Usage
    requested_model: str
    served_model: str  # differs from requested_model when a refusal fallback ran
    request_id: str | None = None
    refusal_category: str | None = None
    fallback_used: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    """A provider failure the agent cannot recover from inside this request."""

    def __init__(self, message: str, *, kind: str, retryable: bool, status_code: int | None = None):
        super().__init__(message)
        self.kind = kind  # rate_limit | server | timeout | connection | bad_request | auth
        self.retryable = retryable
        self.status_code = status_code


class LLMClient(Protocol):
    provider: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        request_id: str,
        step: int,
    ) -> LLMResponse: ...
