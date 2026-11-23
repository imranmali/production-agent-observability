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
import fcntl
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from canary.analysis import Prometheus, Verdict, canary_alerts, collect, evaluate, load_policy

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


@contextmanager
def state_lock() -> Iterator[None]:
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def read_state() -> dict[str, Any]:
    path = STATE / "release.json"
    return json.loads(path.read_text()) if path.exists() else {"stable": None, "canary": None, "history": []}


def write_state(state: dict[str, Any]) -> None:
    tmp = STATE / "release.tmp"
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.replace(STATE / "release.json")


def clear_canary(state: dict[str, Any]) -> None:
    state["canary"] = None
    state.pop("attempt_id", None)
    state.pop("replacing_stable", None)


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
    weight_tmp = STATE / "canary_weight.tmp"
    weight_tmp.write_text(str(canary_pct))
    weight_tmp.replace(STATE / "canary_weight")


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
                ready = httpx.get(f"{GATEWAY}/readyz", headers={"X-Canary": pin}, timeout=3)
                if ready.status_code == 200:
                    # Give Traefik's health check time to mark the server up.
                    time.sleep(6)
                    return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError(f"{track} did not become ready with {release} in {timeout_s:.0f}s")


def replace_track(track: str, release: str) -> None:
    write_env(track, release)
    recreate(f"agent-{track}")
    wait_ready(track, release)


# --- commands -----------------------------------------------------------


def cmd_init(release: str) -> None:
    with state_lock():
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
    gate = policy["eval_gate"]
    ok = report["pass_rate"] >= gate["min_pass_rate"] and report["guardrail_trips"] <= gate["max_guardrail_trips"]
    log(
        "eval_gate",
        release=release,
        passed=ok,
        pass_rate=round(report["pass_rate"], 3),
        guardrail_trips=report["guardrail_trips"],
        cases=report["total"],
        failures=report["failures"],
    )
    return ok


def cmd_deploy(release: str, skip_evals: bool = False) -> int:
    policy = load_policy()
    with state_lock():
        state = read_state()
        if state["canary"]:
            sys.exit(f"canary {state['canary']} already in progress; run `rollback` first")
        stable = state["stable"]
        if not stable:
            sys.exit("no stable release; run `make up` first")
        if release == stable:
            sys.exit(f"{release} is already stable")
        write_env("canary", release)
        attempt_id = uuid4().hex
        state.update(canary=release, attempt_id=attempt_id)
        write_state(state)

    try:
        return run_deployment(release, stable, attempt_id, policy, skip_evals)
    except (Exception, KeyboardInterrupt) as error:
        try:
            with state_lock():
                if read_state().get("attempt_id") == attempt_id:
                    do_rollback(release, stable, f"controller error: {type(error).__name__} {error}".strip())
        except (Exception, KeyboardInterrupt) as rollback_error:
            print(f"rollback did not complete: {rollback_error}. Run `make rollback` to retry.", file=sys.stderr)
        raise


def run_deployment(release: str, stable: str, attempt_id: str, policy: dict[str, Any], skip_evals: bool) -> int:
    prom = Prometheus(PROM_URL)
    canary_started = datetime.now(UTC)
    with state_lock():
        if read_state().get("attempt_id") != attempt_id:
            return 1
        log("canary_start", release=release, stable=stable)
        annotate(f"canary {release} started (stable {stable})", ["canary", release])
        recreate("agent-canary")
        wait_ready("canary", release)

    if not skip_evals and not run_eval_gate(release, policy):
        return finish_deployment(release, stable, attempt_id, "eval gate failed")

    for weight in policy["steps"]:
        with state_lock():
            if read_state().get("attempt_id") != attempt_id:
                return 1
            write_weights(weight)
        log("step_start", release=release, weight=weight, duration_s=policy["step_duration_s"])
        annotate(f"canary {release} -> {weight}%", ["canary", release])
        extensions = 0
        window = policy["step_duration_s"]
        while True:
            deadline = time.monotonic() + window
            while time.monotonic() < deadline:
                time.sleep(5)
                if read_state().get("attempt_id") != attempt_id:
                    return 1
                firing = canary_alerts(prom.firing_alerts(), release, canary_started)
                if firing and policy["analysis"]["block_on_critical_alerts"]:
                    # don't wait out the step while a page is firing
                    break
            time.sleep(policy["settle_s"])
            elapsed = policy["step_duration_s"] * (extensions + 1) + policy["settle_s"]
            verdict = analyse(prom, release, stable, elapsed, policy, canary_started)
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
            return finish_deployment(release, stable, attempt_id, f"step {weight}%: {verdict.reason}")

    return finish_deployment(release, stable, attempt_id)


def finish_deployment(release: str, stable: str, attempt_id: str, reason: str | None = None) -> int:
    with state_lock():
        if read_state().get("attempt_id") != attempt_id:
            return 1
        if reason is not None:
            do_rollback(release, stable, reason)
            return 1
        return do_promote(release, stable)


def analyse(
    prom: Prometheus, release: str, stable: str, window_s: float, policy: dict[str, Any], since: datetime
) -> Verdict:
    canary_m = collect(prom, "canary", release, int(window_s))
    stable_m = collect(prom, "stable", stable, int(window_s))
    firing = canary_alerts(prom.firing_alerts(), release, since)
    return evaluate(canary_m, stable_m, policy, firing)


def do_promote(release: str, previous: str) -> int:
    log("promote_start", release=release, previous=previous)
    write_weights(100)
    time.sleep(3)
    state = read_state()
    state["replacing_stable"] = True
    write_state(state)
    replace_track("stable", release)
    write_weights(0)
    clear_canary(state)
    state["stable"] = release
    state["history"].append(release)
    write_state(state)
    log("promoted", release=release, previous=previous)
    annotate(f"promoted {release} to stable", ["promote", release])
    return 0


def do_rollback(release: str, stable: str, reason: str) -> None:
    started = time.monotonic()
    state = read_state()
    # Revoke the deployment before cleanup. If cleanup fails, the original
    # process must not increase the canary weight again.
    state.pop("attempt_id", None)
    write_state(state)
    if state.get("replacing_stable"):
        # Promotion may have replaced stable. Keep the ready canary serving
        # while restoring the previous stable release before switching back.
        write_weights(100)
        time.sleep(3)
        replace_track("stable", stable)
    routing_started = time.monotonic()
    write_weights(0)  # remove the canary from weighted routing before resetting it
    log(
        "rollback",
        release=release,
        stable=stable,
        reason=reason,
        routing_config_write_s=round(time.monotonic() - routing_started, 3),
    )
    annotate(f"ROLLBACK {release}: {reason}", ["rollback", release])
    write_env("canary", stable)
    recreate("agent-canary")
    clear_canary(state)
    write_state(state)
    log("rollback_done", release=release, rollback_duration_s=round(time.monotonic() - started, 2))


def cmd_rollback() -> int:
    with state_lock():
        state = read_state()
        if state["canary"]:
            do_rollback(state["canary"], state["stable"], "manual rollback")
            return 0
        history = state["history"]
        if len(history) < 2:
            sys.exit("no previous stable release to roll back to")
        current, previous = history[-1], history[-2]
        log("stable_rollback_start", current=current, target=previous)
        annotate(f"ROLLBACK stable {current} -> {previous}", ["rollback", current])
        replace_track("canary", previous)
        write_weights(100)
        time.sleep(3)
        replace_track("stable", previous)
        write_weights(0)
        state["stable"] = previous
        state["history"] = history[:-1]
        write_state(state)
        log("stable_rollback_done", current=previous, reverted=current)
        return 0


def cmd_status() -> int:
    state = read_state()
    weight = (STATE / "canary_weight").read_text() if (STATE / "canary_weight").exists() else "0"
    status = {name: state[name] for name in ("stable", "canary", "history")}
    print(json.dumps({**status, "canary_weight_pct": int(weight)}, indent=2))
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
