"""The compose controller and Argo Rollouts must enforce the same canary gates,
and generated artefacts must match their sources."""

import json
import subprocess
import sys
from pathlib import Path

import yaml

from canary.analysis import load_policy

ROOT = Path(__file__).resolve().parent.parent


def test_argo_analysis_thresholds_match_policy():
    template = yaml.safe_load((ROOT / "deploy/k8s/analysis-template.yaml").read_text())
    args = {a["name"]: a.get("value") for a in template["spec"]["args"]}
    a = load_policy()["analysis"]
    assert float(args["max-failure-ratio"]) == a["max_failure_ratio"]
    assert float(args["max-failure-ratio-delta"]) == a["max_failure_ratio_delta"]
    assert float(args["max-loop-ratio"]) == a["max_loop_ratio"]
    assert float(args["max-p95-latency-ratio"]) == a["max_p95_latency_ratio"]
    assert float(args["max-cost-per-request-ratio"]) == a["max_cost_per_request_ratio"]
    assert int(args["min-canary-requests"]) == a["min_canary_requests"]


def test_eval_gate_pass_rate_matches_policy():
    template = yaml.safe_load((ROOT / "deploy/k8s/eval-gate-template.yaml").read_text())
    args = {a["name"]: a.get("value") for a in template["spec"]["args"]}
    gate = load_policy()["eval_gate"]
    assert float(args["min-pass-rate"]) == gate["min_pass_rate"]
    assert int(args["max-guardrail-trips"]) == gate["max_guardrail_trips"]


def test_rollout_steps_match_policy_weights():
    rollout = yaml.safe_load((ROOT / "deploy/k8s/rollout.yaml").read_text())
    weights = [s["setWeight"] for s in rollout["spec"]["strategy"]["canary"]["steps"] if "setWeight" in s]
    assert weights == load_policy()["steps"]


def test_k8s_prometheus_rules_are_generated():
    r = subprocess.run([sys.executable, "scripts/gen_k8s_rules.py", "--check"], cwd=ROOT, capture_output=True)
    assert r.returncode == 0, r.stderr.decode()


def test_dashboards_are_generated():
    sys.path.insert(0, str(ROOT / "scripts"))
    import build_dashboards

    committed = json.loads((ROOT / "deploy/grafana/dashboards/agent-overview.json").read_text())
    assert committed == build_dashboards.overview()
    committed = json.loads((ROOT / "deploy/grafana/dashboards/agent-canary.json").read_text())
    assert committed == build_dashboards.canary()


def test_every_release_file_parses():
    for path in (ROOT / "releases").glob("*.env"):
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                assert key.isupper() and value, f"{path.name}: bad line {line!r}"
