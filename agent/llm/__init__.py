from agent.config import Settings
from agent.llm.base import LLMClient, LLMError, LLMResponse, ToolCall


def make_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "anthropic":
        from agent.llm.anthropic_client import AnthropicClient

        return AnthropicClient(settings)
    from agent.llm.simulated import SimulatedClient

    return SimulatedClient(settings)


__all__ = ["LLMClient", "LLMError", "LLMResponse", "ToolCall", "make_client"]
