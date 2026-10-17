"""Canary analysis: compare the canary with stable over one step's window.

`evaluate` is a pure function over two `TrackMetrics` so the decision logic is
unit-tested without Prometheus; `collect` runs the PromQL.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

POLICY_PATH = Path(__file__).parent / "policy.yaml"


def load_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


@dataclass
class TrackMetrics:
    requests: float
    failures: float
    loops: float
    p95_latency_s: float | None
    cost_usd: float

    @property
    def failure_ratio(self) -> float:
        return self.failures / self.requests if self.requests else 0.0

    @property
    def loop_ratio(self) -> float:
        return self.loops / self.requests if self.requests else 0.0

    @property
    def cost_per_request(self) -> float:
        return self.cost_usd / self.requests if self.requests else 0.0


@dataclass
class Check:
    name: str
    passed: bool
    observed: str
    limit: str


@dataclass
class Verdict:
    status: str  # pass | fail | inconclusive
    checks: list[Check] = field(default_factory=list)
    reason: str = ""

    def summary(self) -> str:
        lines = [f"verdict: {self.status.upper()}" + (f" ({self.reason})" if self.reason else "")]
        for c in self.checks:
            lines.append(f"  [{'ok' if c.passed else 'FAIL'}] {c.name}: {c.observed} (limit {c.limit})")
        return "\n".join(lines)


def evaluate(
    canary: TrackMetrics,
    stable: TrackMetrics,
    policy: dict[str, Any],
    firing_critical: list[str] | None = None,
) -> Verdict:
    a = policy["analysis"]
    checks: list[Check] = []

    if a.get("block_on_critical_alerts", True) and firing_critical:
        checks.append(Check("critical alerts on canary", False, ", ".join(firing_critical), "none"))
        return Verdict("fail", checks, "critical alert firing")

    if canary.requests < a["min_canary_requests"]:
        return Verdict("inconclusive", checks, f"{canary.requests:.0f} canary requests < {a['min_canary_requests']}")

    limit = min(a["max_failure_ratio"], stable.failure_ratio + a["max_failure_ratio_delta"])
    limit = max(limit, a["max_failure_ratio_delta"])  # a perfect stable must not demand a perfect canary
    checks.append(Check("failure ratio", canary.failure_ratio <= limit, f"{canary.failure_ratio:.2%}", f"{limit:.2%}"))
    checks.append(
        Check(
            "loop ratio",
            canary.loop_ratio <= a["max_loop_ratio"],
            f"{canary.loop_ratio:.2%}",
            f"{a['max_loop_ratio']:.2%}",
        )
    )

    if canary.p95_latency_s is not None and stable.p95_latency_s:
        lat_limit = max(
            stable.p95_latency_s * a["max_p95_latency_ratio"], stable.p95_latency_s + a["p95_latency_slack_s"]
        )
        checks.append(
            Check(
                "p95 latency",
                canary.p95_latency_s <= lat_limit,
                f"{canary.p95_latency_s:.2f}s vs stable {stable.p95_latency_s:.2f}s",
                f"{lat_limit:.2f}s",
            )
        )

    if stable.cost_per_request > 0:
        cost_limit = stable.cost_per_request * a["max_cost_per_request_ratio"]
        checks.append(
            Check(
                "cost per request",
                canary.cost_per_request <= cost_limit,
                f"${canary.cost_per_request:.5f} vs stable ${stable.cost_per_request:.5f}",
                f"${cost_limit:.5f}",
            )
        )

    failed = [c.name for c in checks if not c.passed]
    return Verdict("fail" if failed else "pass", checks, ", ".join(failed))


class Prometheus:
    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def scalar(self, expr: str) -> float | None:
        r = httpx.get(f"{self.url}/api/v1/query", params={"query": expr}, timeout=10)
        r.raise_for_status()
        result = r.json()["data"]["result"]
        if not result:
            return None
        v = float(result[0]["value"][1])
        return None if math.isnan(v) else v

    def firing_alerts(self, **match: str) -> list[dict[str, Any]]:
        r = httpx.get(f"{self.url}/api/v1/alerts", timeout=10)
        r.raise_for_status()
        out = []
        for alert in r.json()["data"]["alerts"]:
            labels = alert["labels"]
            if alert["state"] == "firing" and all(labels.get(k) == v for k, v in match.items()):
                out.append(alert)
        return out


def _parse_ts(value: str) -> datetime:
    """Prometheus timestamps carry nanoseconds; datetime takes microseconds."""
    value = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    return datetime.fromisoformat(value)


def canary_alerts(alerts: list[dict[str, Any]], release: str, since: datetime) -> list[str]:
    """Critical alerts that belong to *this* canary: track=canary, the same release (or
    no release label, as on the canary-vs-stable comparisons), and active only since
    this canary started. Alerts on 5-minute windows keep firing for minutes after a
    rollback; without the time filter they would fail the next, unrelated canary."""
    out = []
    for a in alerts:
        labels = a["labels"]
        if labels.get("track") != "canary" or labels.get("severity") != "critical":
            continue
        if labels.get("release") not in (None, release):
            continue
        if _parse_ts(a["activeAt"]) < since:
            continue
        out.append(labels["alertname"])
    return out


def collect(prom: Prometheus, track: str, release: str, window_s: int) -> TrackMetrics:
    sel = f'track="{track}",release="{release}"'
    w = f"{int(window_s)}s"
    return TrackMetrics(
        requests=prom.scalar(f"sum(increase(agent_requests_total{{{sel}}}[{w}]))") or 0.0,
        failures=prom.scalar(f'sum(increase(agent_requests_total{{{sel},outcome!="success"}}[{w}]))') or 0.0,
        loops=prom.scalar(f'sum(increase(agent_guardrail_trips_total{{{sel},guardrail="loop_detected"}}[{w}]))') or 0.0,
        p95_latency_s=prom.scalar(
            f"histogram_quantile(0.95, sum by (le) (rate(agent_request_duration_seconds_bucket{{{sel}}}[{w}])))"
        ),
        cost_usd=prom.scalar(f"sum(increase(agent_llm_cost_usd_total{{{sel}}}[{w}]))") or 0.0,
    )
