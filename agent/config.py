"""Runtime configuration, loaded from environment variables.

A *release* is the unit the canary controller ships: an image tag plus the
settings in `releases/<name>.env` (prompt version, model, guardrail limits).
Everything here can be set per release.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Identity: shows up on every metric, span and log line.
    service_name: str = "support-agent"
    release: str = Field("dev", description="Release name, e.g. v1.4.0")
    # stable | canary under docker compose; on Kubernetes Argo Rollouts tells the
    # tracks apart by pod-template hash instead (see deploy/k8s/servicemonitor.yaml).
    deployment_track: str = "dev"
    environment: str = "local"

    # Model provider.
    llm_provider: Literal["anthropic", "simulated"] = "simulated"
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_tokens: int = 16000
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2
    # Server-side refusal fallback (Claude API only).
    enable_refusal_fallback: bool = True
    prompt_version: str = "v1"

    # Guardrails.
    max_steps: int = 8
    max_identical_tool_calls: int = 3
    max_request_tokens: int = 60_000
    max_request_cost_usd: float = 0.50
    request_timeout_s: float = 90.0

    # Simulated provider knobs (fault injection for drills and canary demos).
    sim_seed: int = 7
    sim_base_latency_ms: float = 350.0
    sim_latency_per_output_token_ms: float = 6.0
    sim_latency_multiplier: float = 1.0
    sim_error_rate: float = 0.0
    sim_loop_rate: float = 0.0
    sim_verbosity: float = 1.0

    # Telemetry.
    project_name: str = "production-agent"  # Phoenix / Arize / LangSmith project
    otel_exporter_otlp_endpoint: str | None = None  # e.g. http://otel-collector:4318
    otel_console: bool = False
    # Export prompts / responses / tool I/O on spans. On for local runs; the k8s
    # manifests turn it off unless the trace backend is approved to hold customer text.
    trace_capture_content: bool = True
    trace_max_attr_chars: int = 4000
    # Direct-to-vendor exporters (used when there is no collector in front).
    langsmith_api_key: str | None = None
    langsmith_otlp_endpoint: str = "https://api.smith.langchain.com/otel/v1/traces"
    phoenix_collector_endpoint: str | None = None  # e.g. http://phoenix:6006/v1/traces
    anthropic_sdk_tracing: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
