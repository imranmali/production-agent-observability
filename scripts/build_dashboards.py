"""Generate the Grafana dashboards from code so panels stay consistent and
reviewable. `make dashboards` rewrites deploy/grafana/dashboards/*.json; CI fails
if the committed JSON is stale.

Colour: stable and canary keep the same hue on every panel (stable = blue,
canary = orange); red/amber are reserved for thresholds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent.parent / "deploy" / "grafana" / "dashboards"
DS = {"type": "prometheus", "uid": "prometheus"}
STABLE, CANARY = "#3987e5", "#d95926"
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]


class Layout:
    def __init__(self) -> None:
        self.y = 0
        self.x = 0
        self.row_h = 0
        self.next_id = 1

    def place(self, w: int, h: int) -> dict[str, int]:
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        pos = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.x += w
        self.row_h = max(self.row_h, h)
        return pos

    def row(self, title: str) -> dict[str, Any]:
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        panel = {
            "type": "row",
            "title": title,
            "id": self._id(),
            "collapsed": False,
            "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1},
            "panels": [],
        }
        self.y += 1
        return panel

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id


def target(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict[str, Any]:
    return {
        "datasource": DS,
        "expr": expr,
        "legendFormat": legend,
        "refId": ref,
        "instant": instant,
        "range": not instant,
    }


def track_overrides() -> list[dict[str, Any]]:
    return [
        {
            "matcher": {"id": "byRegexp", "options": f".*{name}.*"},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
        }
        for name, color in (("stable", STABLE), ("canary", CANARY))
    ]


def stat(
    L: Layout,
    title: str,
    expr: str,
    unit: str,
    thresholds: list[tuple[str, float | None]],
    desc: str,
    w: int = 4,
    decimals: int | None = None,
) -> dict[str, Any]:
    p = {
        "type": "stat",
        "id": L._id(),
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": L.place(w, 4),
        "targets": [target(expr, instant=True)],
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": "none",
            "textMode": "value",
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"color": c, "value": v} for c, v in thresholds],
                },
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
    }
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    return p


def ts(
    L: Layout,
    title: str,
    targets: list[dict[str, Any]],
    unit: str,
    desc: str,
    w: int = 12,
    h: int = 8,
    stack: bool = False,
    by_track: bool = True,
    threshold: float | None = None,
    min0: bool = True,
    soft_max: float | None = None,
) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "unit": unit,
        "color": {"mode": "palette-classic"},
        "custom": {
            "lineWidth": 2,
            "fillOpacity": 25 if stack else 0,
            "showPoints": "never",
            "spanNulls": True,
            "stacking": {"mode": "normal" if stack else "none"},
            "axisSoftMin": 0 if min0 else None,
            "axisSoftMax": soft_max if soft_max is not None else (threshold * 2 if threshold else None),
            "thresholdsStyle": {"mode": "dashed" if threshold is not None else "off"},
        },
    }
    if threshold is not None:
        defaults["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": "transparent", "value": None}, {"color": "#e34948", "value": threshold}],
        }
    return {
        "type": "timeseries",
        "id": L._id(),
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": L.place(w, h),
        "targets": targets,
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "fieldConfig": {"defaults": defaults, "overrides": track_overrides() if by_track else []},
    }


def dashboard(uid: str, title: str, panels: list[dict[str, Any]], desc: str) -> dict[str, Any]:
    return {
        "uid": uid,
        "title": title,
        "description": desc,
        "tags": ["agent", "llm"],
        "timezone": "browser",
        "schemaVersion": 41,
        "version": 1,
        "editable": True,
        "refresh": "10s",
        "time": {"from": "now-30m", "to": "now"},
        "graphTooltip": 1,
        "annotations": {
            "list": [
                {
                    "builtIn": 1,
                    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                    "enable": True,
                    "hide": True,
                    "iconColor": "rgba(0, 211, 255, 1)",
                    "name": "Annotations & Alerts",
                    "type": "dashboard",
                },
                {
                    "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                    "enable": True,
                    "iconColor": "#9085e9",
                    "name": "Deploys",
                    "target": {"type": "tags", "tags": ["deploy"], "matchAny": True, "limit": 100},
                },
            ]
        },
        "panels": panels,
        "templating": {"list": []},
    }


def overview() -> dict[str, Any]:
    L = Layout()
    P: list[dict[str, Any]] = []
    green, amber, red = "#1baf7a", "#eda100", "#e34948"

    P.append(L.row("Service level (last 5m, all tracks)"))
    P += [
        stat(
            L,
            "Requests / s",
            "sum(agent:requests:rate5m)",
            "reqps",
            [("text", None)],
            "Agent requests per second.",
            decimals=2,
        ),
        stat(
            L,
            "Success rate",
            "1 - sum(agent:failures:rate5m) / sum(agent:requests:rate5m)",
            "percentunit",
            [(red, None), (amber, 0.95), (green, 0.99)],
            "Share of requests with outcome=success. SLO: 99%.",
            decimals=1,
        ),
        stat(
            L,
            "p95 latency",
            "histogram_quantile(0.95, sum by (le) (rate(agent_request_duration_seconds_bucket[5m])))",
            "s",
            [(green, None), (amber, 15), (red, 20)],
            "End-to-end request latency, 95th percentile. Alert at 20s.",
            decimals=2,
        ),
        stat(
            L,
            "Spend / hour",
            "sum(rate(agent_llm_cost_usd_total[5m])) * 3600",
            "currencyUSD",
            [(green, None), (amber, 40), (red, 60)],
            "Model spend at the current rate. Budget alert at $60/hour.",
            decimals=2,
        ),
        stat(
            L,
            "Cost / request",
            "sum(rate(agent_llm_cost_usd_total[5m])) / sum(rate(agent_requests_total[5m]))",
            "currencyUSD",
            [("text", None)],
            "Average model spend per agent request.",
            decimals=4,
        ),
        stat(
            L,
            "Loop rate",
            '(sum(rate(agent_guardrail_trips_total{guardrail="loop_detected"}[5m])) or vector(0))'
            " / sum(rate(agent_requests_total[5m]))",
            "percentunit",
            [(green, None), (amber, 0.005), (red, 0.02)],
            "Requests stopped by the loop detector. Page above 2%.",
            decimals=2,
        ),
    ]

    P.append(L.row("Traffic and outcomes"))
    P.append(
        ts(
            L,
            "Requests / s by track",
            [target("sum by (track) (agent:requests:rate5m)", "{{track}}")],
            "reqps",
            "Traffic split between stable and canary.",
        )
    )
    P.append(
        ts(
            L,
            "Failures / s by outcome",
            [target('sum by (outcome) (rate(agent_requests_total{outcome!="success"}[5m]))', "{{outcome}}")],
            "reqps",
            "Every non-success outcome: guardrail trips, provider errors, refusals, exceptions.",
            stack=True,
            by_track=False,
        )
    )
    P.append(
        ts(
            L,
            "Failure ratio by track",
            [target("sum by (track) (agent:failure_ratio:rate5m)", "{{track}}")],
            "percentunit",
            "Non-success share per track. Alert at 5%.",
            threshold=0.05,
        )
    )
    P.append(
        ts(
            L,
            "Guardrail trips / min",
            [target("sum by (guardrail) (rate(agent_guardrail_trips_total[5m])) * 60", "{{guardrail}}")],
            "short",
            "Loop detector, step budget, token/cost budget and timeout trips.",
            stack=True,
            by_track=False,
        )
    )

    P.append(L.row("Latency"))
    P.append(
        ts(
            L,
            "Request latency p50 / p95 by track",
            [
                target("sum by (track) (agent:latency_seconds:p50_5m)", "p50 {{track}}", "A"),
                target("sum by (track) (agent:latency_seconds:p95_5m)", "p95 {{track}}", "B"),
            ],
            "s",
            "End-to-end agent latency (all model calls and tools).",
        )
    )
    P.append(
        ts(
            L,
            "Model call latency p95 by model",
            [
                target(
                    "histogram_quantile(0.95, sum by (le, model) (rate(agent_llm_latency_seconds_bucket[5m])))",
                    "{{model}}",
                )
            ],
            "s",
            "Latency of a single Messages API call.",
            by_track=False,
        )
    )

    P.append(L.row("Cost and tokens"))
    P.append(
        ts(
            L,
            "Spend per hour by track",
            [target("sum by (track) (agent:cost_usd:rate5m) * 3600", "{{track}}")],
            "currencyUSD",
            "Model spend extrapolated to an hour.",
            stack=True,
        )
    )
    P.append(
        ts(
            L,
            "Cost per request by track",
            [target("sum by (track) (agent:cost_per_request_usd:rate5m)", "{{track}}")],
            "currencyUSD",
            "Average spend per request; the canary gate allows +20%.",
        )
    )
    P.append(
        ts(
            L,
            "Tokens / s by type",
            [target("sum by (type) (rate(agent_llm_tokens_total[5m]))", "{{type}}")],
            "short",
            "Input, output, cache-read and cache-write tokens.",
            stack=True,
            by_track=False,
        )
    )
    P.append(
        ts(
            L,
            "Prompt cache hit ratio",
            [
                target(
                    'sum(rate(agent_llm_tokens_total{type="cache_read"}[5m]))'
                    ' / sum(rate(agent_llm_tokens_total{type=~"input|cache_read|cache_write"}[5m]))',
                    "cache read share of input",
                )
            ],
            "percentunit",
            "Share of input tokens served from the prompt cache.",
            by_track=False,
        )
    )

    P.append(L.row("Agent behaviour"))
    P.append(
        ts(
            L,
            "Model calls per request by track",
            [target("sum by (track) (agent:steps_per_request:avg5m)", "{{track}}")],
            "short",
            "Average agent steps (model calls) per request. Rising = the agent is wandering.",
            w=8,
        )
    )
    P.append(
        ts(
            L,
            "Tool calls / s by tool",
            [target("sum by (tool) (rate(agent_tool_calls_total[5m]))", "{{tool}}")],
            "reqps",
            "Tool usage mix.",
            w=8,
            stack=True,
            by_track=False,
        )
    )
    P.append(
        ts(
            L,
            "Tool error ratio by tool",
            [
                target(
                    'sum by (tool) (rate(agent_tool_calls_total{status="error"}[5m]))'
                    " / sum by (tool) (rate(agent_tool_calls_total[5m]))",
                    "{{tool}}",
                )
            ],
            "percentunit",
            "Tool calls that returned an error to the model.",
            w=8,
            by_track=False,
        )
    )

    P.append(L.row("SLO: 99% of requests succeed (30 days)"))
    P.append(
        stat(
            L,
            "Error budget remaining (30d)",
            '1 - (sum(increase(agent_requests_total{outcome!="success"}[30d]))'
            " / sum(increase(agent_requests_total[30d]))) / 0.01",
            "percentunit",
            [(red, None), (amber, 0.25), (green, 0.5)],
            "Share of the 1% error budget left over the last 30 days.",
            w=6,
            decimals=1,
        )
    )
    P.append(
        ts(
            L,
            "Burn rate (1h and 6h windows)",
            [
                target("agent:slo_error_ratio:rate1h / 0.01", "1h burn", "A"),
                target("agent:slo_error_ratio:rate6h / 0.01", "6h burn", "B"),
            ],
            "short",
            "1 = spending the budget exactly over 30 days. Page at 14.4 (1h), ticket at 6 (6h).",
            w=18,
            by_track=False,
            threshold=14.4,
        )
    )
    return dashboard("agent-overview", "Agent overview", P, "Latency, cost, failures and loops of the support agent.")


def canary() -> dict[str, Any]:
    L = Layout()
    P: list[dict[str, Any]] = []
    P.append(L.row("Stable vs canary (same SLIs the canary analysis uses)"))
    pairs = [
        (
            "Traffic share",
            "sum by (track) (agent:requests:rate5m) / scalar(sum(agent:requests:rate5m))",
            "percentunit",
            None,
            "Share of requests each track serves (Traefik weights).",
        ),
        (
            "Failure ratio",
            "sum by (track) (agent:failure_ratio:rate5m)",
            "percentunit",
            0.05,
            "Gate: canary <= stable + 2pp and <= 5%.",
        ),
        (
            "Loop ratio",
            "sum by (track) (agent:loop_ratio:rate5m)",
            "percentunit",
            0.01,
            "Gate: canary <= 1%.",
        ),
        (
            "p95 latency",
            "sum by (track) (agent:latency_seconds:p95_5m)",
            "s",
            None,
            "Gate: canary <= 1.25x stable.",
        ),
        (
            "Cost per request",
            "sum by (track) (agent:cost_per_request_usd:rate5m)",
            "currencyUSD",
            None,
            "Gate: canary <= 1.2x stable.",
        ),
        (
            "Model calls per request",
            "sum by (track) (agent:steps_per_request:avg5m)",
            "short",
            None,
            "Average agent steps per request.",
        ),
    ]
    for title, expr, unit, thr, desc in pairs:
        P.append(ts(L, title, [target(expr, "{{track}}")], unit, desc, w=12, h=7, threshold=thr))
    P.append(L.row("Releases and alerts"))
    P.append(
        {
            "type": "table",
            "id": L._id(),
            "title": "Running releases",
            "description": "agent_build_info per track.",
            "datasource": DS,
            "gridPos": L.place(10, 6),
            "targets": [
                target(
                    "max by (track, release, model, prompt_version, provider) (agent_build_info)",
                    "",
                    instant=True,
                )
            ],
            "transformations": [
                {"id": "labelsToFields", "options": {"mode": "columns"}},
                {"id": "organize", "options": {"excludeByName": {"Time": True, "Value": True}}},
            ],
            "options": {"showHeader": True},
            "fieldConfig": {"defaults": {}, "overrides": []},
        }
    )
    P.append(
        {
            "type": "table",
            "id": L._id(),
            "title": "Firing alerts",
            "description": "Prometheus alerts currently firing (ALERTS series).",
            "datasource": DS,
            "gridPos": L.place(14, 6),
            "targets": [target('ALERTS{alertstate="firing"}', "", instant=True)],
            "transformations": [
                {"id": "labelsToFields", "options": {"mode": "columns"}},
                {
                    "id": "organize",
                    "options": {"excludeByName": {"Time": True, "Value": True, "__name__": True, "alertstate": True}},
                },
            ],
            "options": {"showHeader": True},
            "fieldConfig": {"defaults": {}, "overrides": []},
        }
    )
    return dashboard(
        "agent-canary", "Agent canary analysis", P, "Stable vs canary on every gate the controller checks."
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, d in (("agent-overview.json", overview()), ("agent-canary.json", canary())):
        (OUT / name).write_text(json.dumps(d, indent=2) + "\n")
        print(f"wrote {OUT / name}")


if __name__ == "__main__":
    main()
