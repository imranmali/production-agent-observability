"""Claude via the official Anthropic Python SDK.

The agent owns the loop (see agent/runner.py) instead of using the SDK's tool
runner, because every step has to pass through the guardrails and emit its own
telemetry before the next request is sent.
"""

from __future__ import annotations

from typing import Any

import anthropic

from agent.config import Settings
from agent.llm.base import LLMError, LLMResponse, ToolCall
from agent.pricing import Usage

REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicClient:
    provider = "anthropic"

    def __init__(self, settings: Settings):
        self.model = settings.model
        self._settings = settings
        self._client = anthropic.AsyncAnthropic(
            timeout=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            # The agent emits its own LLM span (OpenInference + GenAI attributes) for
            # every provider; SDK spans would double-count tokens in LangSmith/Phoenix.
            open_telemetry=None if settings.anthropic_sdk_tracing else False,
        )

    async def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        request_id: str,
        step: int,
    ) -> LLMResponse:
        s = self._settings
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": s.max_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
            "output_config": {"effort": s.effort},
            # Caches the longest stable prefix (tools + system + earlier turns).
            "cache_control": {"type": "ephemeral"},
            "metadata": {"user_id": request_id},
        }
        if s.enable_refusal_fallback:
            kwargs["betas"] = [REFUSAL_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            msg = await self._client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            raise LLMError(str(e), kind="rate_limit", retryable=True, status_code=429) from e
        except anthropic.AuthenticationError as e:
            raise LLMError(str(e), kind="auth", retryable=False, status_code=401) from e
        except anthropic.BadRequestError as e:
            raise LLMError(str(e), kind="bad_request", retryable=False, status_code=400) from e
        except anthropic.APITimeoutError as e:
            raise LLMError(str(e), kind="timeout", retryable=True) from e
        except anthropic.APIStatusError as e:
            kind = "server" if e.status_code >= 500 else "client"
            raise LLMError(
                str(e), kind=kind, retryable=e.status_code >= 500, status_code=e.status_code
            ) from e
        except anthropic.APIConnectionError as e:
            raise LLMError(str(e), kind="connection", retryable=True) from e

        u = msg.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        tool_calls = [
            ToolCall(id=b.id, name=b.name, input=dict(b.input)) for b in msg.content if b.type == "tool_use"
        ]
        text = "".join(b.text for b in msg.content if b.type == "text")
        fallback_used = any(b.type == "fallback" for b in msg.content) or any(
            getattr(it, "type", None) == "fallback_message" for it in (getattr(u, "iterations", None) or [])
        )
        refusal_category = None
        if msg.stop_reason == "refusal" and getattr(msg, "stop_details", None):
            refusal_category = msg.stop_details.category
        return LLMResponse(
            content=list(msg.content),
            text=text,
            tool_calls=tool_calls,
            stop_reason=msg.stop_reason or "unknown",
            usage=usage,
            requested_model=self.model,
            served_model=msg.model,
            request_id=getattr(msg, "_request_id", None),
            refusal_category=refusal_category,
            fallback_used=fallback_used,
        )
