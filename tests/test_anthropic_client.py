"""The Claude client against a mocked HTTP transport: checks the request the SDK
actually sends and how the response is mapped, without an API key or network."""

import json

import anthropic
import httpx2
import pytest

from agent.config import Settings
from agent.llm.anthropic_client import REFUSAL_FALLBACK_BETA, AnthropicClient
from agent.llm.base import LLMError
from agent.tools import tool_specs


def client_with(handler) -> AnthropicClient:
    c = AnthropicClient(Settings(_env_file=None, llm_provider="anthropic", effort="low", llm_max_retries=0))
    c._client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        open_telemetry=False,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return c


def message(**overrides):
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [
            {"type": "text", "text": "Let me look that up."},
            {"type": "tool_use", "id": "toolu_1", "name": "get_order", "input": {"order_id": "10232"}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 120,
            "output_tokens": 40,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 0,
        },
    }
    body.update(overrides)
    return body


async def test_request_shape_and_tool_use_mapping():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=message(), headers={"request-id": "req_123"})

    resp = await client_with(handler).complete(
        system="sys",
        tools=tool_specs(),
        messages=[{"role": "user", "content": "Where is 10232?"}],
        request_id="r1",
        step=1,
    )
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"] == {"effort": "low"}
    assert body["fallbacks"] == "default"
    assert REFUSAL_FALLBACK_BETA in seen["headers"]["anthropic-beta"]
    assert body["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] for t in body["tools"])
    assert "thinking" not in body  # Opus 5.5: thinking is always on; effort is the control

    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls[0].name == "get_order" and resp.tool_calls[0].input == {"order_id": "10232"}
    assert resp.usage.cache_read_tokens == 900 and resp.usage.output_tokens == 40
    assert resp.served_model == "claude-opus-5-5" and not resp.fallback_used


async def test_refusal_is_reported_with_category():
    def handler(request):
        return httpx2.Response(
            200,
            json=message(
                content=[],
                stop_reason="refusal",
                stop_details={"type": "refusal", "category": "cyber", "explanation": None},
            ),
        )

    resp = await client_with(handler).complete(system="s", tools=[], messages=[], request_id="r", step=1)
    assert resp.stop_reason == "refusal" and resp.refusal_category == "cyber"


@pytest.mark.parametrize(
    ("status", "kind", "retryable"),
    [(429, "rate_limit", True), (529, "server", True), (401, "auth", False), (400, "bad_request", False)],
)
async def test_api_errors_map_to_llm_errors(status, kind, retryable):
    def handler(request):
        return httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "boom"}})

    with pytest.raises(LLMError) as e:
        await client_with(handler).complete(system="s", tools=[], messages=[], request_id="r", step=1)
    assert e.value.kind == kind and e.value.retryable is retryable
