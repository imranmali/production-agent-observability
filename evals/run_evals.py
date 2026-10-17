"""Golden-set behavioural evals, run against a live deployment.

Used three ways:
  * CI: against a freshly built container (simulated provider)
  * canary eval gate: against the canary via `X-Canary: always`, before any
    customer traffic is shifted (canary/controller.py)
  * by hand against anything: `python -m evals.run_evals --url http://localhost:8080`

Each case checks behaviour, not exact wording: the request succeeds, the
expected tools were called, the answer mentions the key fact ("a|b" = either),
and it took no more than `max_steps` model calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx

GOLDEN = Path(__file__).parent / "golden.jsonl"


def load_cases(path: Path = GOLDEN) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def check_case(case: dict[str, Any], resp: dict[str, Any]) -> list[str]:
    problems = []
    if resp.get("outcome") != "success":
        problems.append(f"outcome={resp.get('outcome')} ({resp.get('detail')})")
    called = {tc["name"] for tc in resp.get("tool_calls", [])}
    missing = [t for t in case.get("expect_tools", []) if t not in called]
    if missing:
        problems.append(f"missing tools {missing} (called {sorted(called)})")
    answer = (resp.get("answer") or "").lower()
    for needle in case.get("expect_contains", []):
        if not any(alt.lower() in answer for alt in needle.split("|")):
            problems.append(f"answer lacks {needle!r}")
    if resp.get("steps", 0) > case.get("max_steps", 99):
        problems.append(f"steps={resp.get('steps')} > {case['max_steps']}")
    return problems


def run_case(client: httpx.Client, url: str, case: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    try:
        r = client.post(
            f"{url}/v1/chat",
            json={"message": case["message"], "session_id": f"eval-{case['id']}"},
            headers=headers,
        )
        resp = r.json()
    except (httpx.HTTPError, json.JSONDecodeError) as e:
        return {"id": case["id"], "passed": False, "problems": [f"request failed: {e}"], "release": None}
    problems = check_case(case, resp)
    return {
        "id": case["id"],
        "passed": not problems,
        "problems": problems,
        "release": resp.get("release"),
        "cost_usd": resp.get("cost_usd", 0.0),
        "duration_s": resp.get("duration_s", 0.0),
        "trace_id": resp.get("trace_id"),
    }


def run_suite(
    url: str,
    headers: dict[str, str] | None = None,
    expect_release: str | None = None,
    concurrency: int = 4,
    path: Path = GOLDEN,
) -> dict[str, Any]:
    cases = load_cases(path)
    headers = {**(headers or {}), "x-eval": "golden"}
    with httpx.Client(timeout=120) as client, ThreadPoolExecutor(concurrency) as pool:
        results = list(pool.map(lambda c: run_case(client, url.rstrip("/"), c, headers), cases))
    if expect_release:
        for r in results:
            if r["release"] not in (None, expect_release):
                r["passed"] = False
                r["problems"].append(f"served by {r['release']}, expected {expect_release}")
    passed = sum(r["passed"] for r in results)
    return {
        "total": len(results),
        "passed": passed,
        "pass_rate": passed / len(results) if results else 0.0,
        "failures": [{"id": r["id"], "problems": r["problems"]} for r in results if not r["passed"]],
        "cost_usd": round(sum(r.get("cost_usd", 0.0) for r in results), 6),
        "results": results,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--canary", action="store_true", help="pin requests to the canary (X-Canary: always)")
    p.add_argument("--min-pass-rate", type=float, default=0.95)
    p.add_argument("--out", type=Path)
    args = p.parse_args()
    report = run_suite(args.url, headers={"X-Canary": "always"} if args.canary else None)
    for r in report["results"]:
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']:<20} {'; '.join(r['problems'])}")
    print(f"\n{report['passed']}/{report['total']} passed ({report['pass_rate']:.0%}), cost ${report['cost_usd']:.4f}")
    if args.out:
        args.out.write_text(json.dumps(report, indent=2))
    sys.exit(0 if report["pass_rate"] >= args.min_pass_rate else 1)


if __name__ == "__main__":
    main()
