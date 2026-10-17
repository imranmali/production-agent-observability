"""Canary controller for the docker-compose deployment.

    python -m canary.controller init   [--release v1.4.0]   seed state for `make up`
    python -m canary.controller deploy RELEASE              eval gate -> 10/25/50% -> promote or roll back
    python -m canary.controller rollback                    abort the canary, or revert stable to the previous release
    python -m canary.controller status

State lives in deploy/state/:
    stable.env, canary.env   release config each container runs (from releases/<name>.env)
    traefik/weights.yml      the traffic split Traefik hot-reloads
    release.json             current stable/canary + history (what `rollback` reverts to)
    events.jsonl             every controller decision, with the analysis numbers

On Kubernetes the same policy runs as an Argo Rollouts AnalysisTemplate
(deploy/k8s/); this module is the dependency-free equivalent for a single host.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from canary.analysis import Prometheus, Verdict, collect, evaluate, load_policy

ROOT = Path(__file__).resolve().parent.parent
RELEASES = ROOT / "releases"
STATE = ROOT / "deploy" / "state"
GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8080")
PROM_URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://localhost:3000")
GRAFANA_AUTH = ("admin", os.environ.get("GRAFANA_ADMIN_PASSWORD", "admin"))


def log(event: str, **fields: Any) -> None:
    entry = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **fields}
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "events.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\n")
    detail = " ".join(f"{k}={v}" for k, v in fields.items() if not isinstance(v, dict | list))
    print(f"[{entry['ts']}] {event} {detail}", flush=True)


def annotate(text: str, tags: list[str]) -> None:
    """Deploy markers on every Grafana panel. Best effort."""
    try:
        httpx.post(
            f"{GRAFANA_URL}/api/annotations",
            json={"text": text, "tags": ["deploy", *tags]},
            auth=GRAFANA_AUTH,
            timeout=3,
        )
    except httpx.HTTPError:
        pass


# --- state ---------------------------------------------------------------


def read_state() -> dict[str, Any]:
    path = STATE / "release.json"
    return json.loads(path.read_text()) if path.exists() else {"stable": None, "canary": None, "history": []}


def write_state(state: dict[str, Any]) -> None:
    (STATE / "release.json").write_text(json.dumps(state, indent=2) + "\n")


def write_env(track: str, release: str) -> None:
    src = RELEASES / f"{release}.env"
    if not src.exists():
        sys.exit(f"unknown release {release}: {src} not found")
    body = src.read_text()
    (STATE / f"{track}.env").write_text(f"RELEASE={release}\n{body}")


def write_weights(canary_pct: int) -> None:
    """Traefik dynamic config. The canary is left out of the weighted service at 0%.
    `X-Canary: always` / `never` pins a request to one track (eval gate, debugging)."""
    services = [{"name": "agent-stable", "weight": 100 - canary_pct}] if canary_pct < 100 else []
    if canary_pct > 0:
        services.append({"name": "agent-canary", "weight": canary_pct})
    health = {"path": "/healthz", "interval": "5s", "timeout": "2s"}
    config = {
        "http": {
            "routers": {
                "agent": {"rule": "PathPrefix(`/`)", "entryPoints": ["web"], "service": "agent-weighted"},
                "agent-canary-pinned": {
                    "rule": "Header(`X-Canary`, `always`)",
                    "entryPoints": ["web"],
                    "service": "agent-canary",
                    "priority": 100,
                },
                "agent-stable-pinned": {
                    "rule": "Header(`X-Canary`, `never`)",
                    "entryPoints": ["web"],
                    "service": "agent-stable",
                    "priority": 100,
                },
            },
            "services": {
                "agent-weighted": {"weighted": {"services": services}},
                "agent-stable": {
                    "loadBalancer": {"servers": [{"url": "http://agent-stable:8080"}], "healthCheck": health}
                },
                "agent-canary": {
                    "loadBalancer": {"servers": [{"url": "http://agent-canary:8080"}], "healthCheck": health}
                },
            },
        }
    }
    path = STATE / "traefik" / "weights.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# Written by canary/controller.py - do not edit by hand.\n" + json.dumps(config, indent=2))
    tmp.replace(path)  # atomic, so Traefik never reads a half-written file
    (STATE / "canary_weight").write_text(str(canary_pct))


# --- compose helpers ----------------------------------------------------


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True)


def recreate(service: str) -> None:
    compose("up", "-d", "--no-deps", "--force-recreate", service)


def wait_ready(track: str, release: str, timeout_s: float = 90) -> None:
    pin = "always" if track == "canary" else "never"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{GATEWAY}/version", headers={"X-Canary": pin}, timeout=3)
            if r.status_code == 200 and r.json().get("release") == release:
                # Give Traefik's health check time to mark the server up.
                time.sleep(6)
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError(f"{track} did not become ready with {release} in {timeout_s:.0f}s")


def swap_stable(release: str) -> None:
    """Replace the stable release without dropping requests: park all traffic on the
    canary container running `release`, recreate stable, then move traffic back."""
    write_env("canary", release)
    recreate("agent-canary")
    wait_ready("canary", release)
    write_weights(100)
    time.sleep(3)
    write_env("stable", release)
    recreate("agent-stable")
    wait_ready("stable", release)
    write_weights(0)


# --- commands -----------------------------------------------------------


def cmd_init(release: str) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    state = read_state()
    if state["stable"]:
        print(f"state exists: stable={state['stable']} (delete deploy/state/ to reset)")
        if not (STATE / "traefik" / "weights.yml").exists():
            write_weights(0)
        return
    write_env("stable", release)
    write_env("canary", release)
    write_weights(0)
    write_state({"stable": release, "canary": None, "history": [release]})
    print(f"initialised: stable={release}")


def run_eval_gate(release: str, policy: dict[str, Any]) -> bool:
    from evals.run_evals import run_suite

    report = run_suite(GATEWAY, headers={"X-Canary": "always"}, expect_release=release)
    ok = report["pass_rate"] >= policy["eval_gate"]["min_pass_rate"]
    log(
        "eval_gate",
        release=release,
        passed=ok,
        pass_rate=round(report["pass_rate"], 3),
        cases=report["total"],
        failures=report["failures"],
    )
    return ok


def cmd_deploy(release: str, skip_evals: bool = False) -> int:
    policy = load_policy()
    state = read_state()
    if state["canary"]:
        sys.exit(f"canary {state['canary']} already in progress; run `rollback` first")
    stable = state["stable"]
    if release == stable:
        sys.exit(f"{release} is already stable")

    prom = Prometheus(PROM_URL)
    log("canary_start", release=release, stable=stable)
    annotate(f"canary {release} started (stable {stable})", ["canary", release])
    write_env("canary", release)
    recreate("agent-canary")
    state["canary"] = release
    write_state(state)
    wait_ready("canary", release)

    if not skip_evals and not run_eval_gate(release, policy):
        return do_rollback(release, stable, "eval gate failed")

    for weight in policy["steps"]:
        write_weights(weight)
        log("step_start", release=release, weight=weight, duration_s=policy["step_duration_s"])
        annotate(f"canary {release} -> {weight}%", ["canary", release])
        extensions = 0
        window = policy["step_duration_s"]
        while True:
            deadline = time.monotonic() + window
            while time.monotonic() < deadline:
                time.sleep(5)
                firing = [
                    a["labels"]["alertname"]
                    for a in prom.firing_alerts(track="canary", severity="critical")
                    if policy["analysis"]["block_on_critical_alerts"]
                ]
                if firing:  # don't wait out the step while a page is firing
                    break
            time.sleep(policy["settle_s"])
            elapsed = policy["step_duration_s"] * (extensions + 1) + policy["settle_s"]
            verdict = analyse(prom, release, stable, elapsed, policy)
            log(
                "step_analysis",
                release=release,
                weight=weight,
                verdict=verdict.status,
                reason=verdict.reason,
                checks=[c.__dict__ for c in verdict.checks],
            )
            print(verdict.summary(), flush=True)
            if verdict.status == "inconclusive" and extensions < policy["max_inconclusive_extensions"]:
                extensions += 1
                continue
            break
        if verdict.status != "pass":
            return do_rollback(release, stable, f"step {weight}%: {verdict.reason}")

    return do_promote(release, stable)


def analyse(prom: Prometheus, release: str, stable: str, window_s: float, policy: dict[str, Any]) -> Verdict:
    canary_m = collect(prom, "canary", release, int(window_s))
    stable_m = collect(prom, "stable", stable, int(window_s))
    firing = [a["labels"]["alertname"] for a in prom.firing_alerts(track="canary", severity="critical")]
    return evaluate(canary_m, stable_m, policy, firing)


def do_promote(release: str, previous: str) -> int:
    log("promote_start", release=release, previous=previous)
    write_weights(100)
    time.sleep(3)
    write_env("stable", release)
    recreate("agent-stable")
    wait_ready("stable", release)
    write_weights(0)
    state = read_state()
    state.update(stable=release, canary=None)
    state["history"].append(release)
    write_state(state)
    log("promoted", release=release, previous=previous)
    annotate(f"promoted {release} to stable", ["promote", release])
    return 0


def do_rollback(release: str, stable: str, reason: str) -> int:
    started = time.monotonic()
    write_weights(0)  # traffic off the canary first; everything else can be slow
    log("rollback", release=release, stable=stable, reason=reason)
    annotate(f"ROLLBACK {release}: {reason}", ["rollback", release])
    write_env("canary", stable)
    recreate("agent-canary")
    state = read_state()
    state["canary"] = None
    write_state(state)
    log("rollback_done", release=release, traffic_shift_s=round(time.monotonic() - started, 2))
    return 1


def cmd_rollback() -> int:
    state = read_state()
    if state["canary"]:
        return do_rollback(state["canary"], state["stable"], "manual rollback")
    history = state["history"]
    if len(history) < 2:
        sys.exit("no previous stable release to roll back to")
    current, previous = history[-1], history[-2]
    log("stable_rollback_start", current=current, target=previous)
    annotate(f"ROLLBACK stable {current} -> {previous}", ["rollback", current])
    swap_stable(previous)
    state.update(stable=previous, canary=None)
    state["history"] = history[:-1]
    write_state(state)
    log("stable_rollback_done", current=previous, reverted=current)
    return 0


def cmd_status() -> int:
    state = read_state()
    weight = (STATE / "canary_weight").read_text() if (STATE / "canary_weight").exists() else "0"
    print(json.dumps({**state, "canary_weight_pct": int(weight)}, indent=2))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="canary.controller")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init")
    i.add_argument("--release", default="v1.4.0")
    d = sub.add_parser("deploy")
    d.add_argument("release")
    d.add_argument("--skip-evals", action="store_true")
    sub.add_parser("rollback")
    sub.add_parser("status")
    args = p.parse_args()
    if args.cmd == "init":
        cmd_init(args.release)
        sys.exit(0)
    if args.cmd == "deploy":
        sys.exit(cmd_deploy(args.release, args.skip_evals))
    if args.cmd == "rollback":
        sys.exit(cmd_rollback())
    sys.exit(cmd_status())


if __name__ == "__main__":
    main()
