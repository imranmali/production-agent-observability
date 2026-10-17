"""Open-loop traffic generator: sends requests at a fixed rate (Poisson arrivals)
regardless of how fast the service answers, like real users do.

    python -m load.loadgen --rps 2 --duration 600
    python -m load.loadgen --rps 2 --duration 0      # run until Ctrl-C

Prints a per-track summary (stable vs canary, from the x-agent-track header).
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
from collections import Counter, defaultdict

import httpx

ORDERS = ["10231", "10232", "10233", "10234", "10235", "10299"]
TEMPLATES = [
    "Where is my order {o}?",
    "What's the status of order #{o}?",
    "Can I return order {o}?",
    "Please cancel order {o}",
    "Order {o} arrived broken, what are my warranty options?",
    "I want to return order {o}. How much do I get back after the fee, {a} - 5.95?",
    "What is your return policy?",
    "How long does a refund take?",
    "How much does shipping cost to Austria?",
    "Do you offer express delivery?",
    "Can I pay by invoice?",
    "I found the keyboard cheaper elsewhere, do you price match?",
    "What is {a} + {b} for my two items?",
    "I want to speak to a human about order {o}",
    "Hi, I have a question",
    "Thanks, that's all!",
]


def make_message(rng: random.Random) -> str:
    return rng.choice(TEMPLATES).format(
        o=rng.choice(ORDERS), a=rng.choice([179, 129, 289, 59]), b=rng.choice([12, 19, 49])
    )


async def main_async(url: str, rps: float, duration: float, seed: int) -> None:
    rng = random.Random(seed)
    stats: dict[str, Counter] = defaultdict(Counter)
    latencies: dict[str, list[float]] = defaultdict(list)
    tasks: set[asyncio.Task] = set()
    sent = 0

    async with httpx.AsyncClient(timeout=120) as client:

        async def one(i: int) -> None:
            started = time.perf_counter()
            try:
                r = await client.post(
                    f"{url}/v1/chat",
                    json={
                        "message": make_message(rng),
                        "session_id": f"load-{i % 500}",
                        "user_id": f"u{i % 97}",
                    },
                )
                track = r.headers.get("x-agent-track", "?")
                outcome = r.json().get("outcome", f"http_{r.status_code}")
            except (httpx.HTTPError, ValueError) as e:
                track, outcome = "?", f"client_{type(e).__name__}"
            stats[track][outcome] += 1
            latencies[track].append(time.perf_counter() - started)

        end = time.monotonic() + duration if duration else float("inf")
        last_report = time.monotonic()
        try:
            while time.monotonic() < end:
                await asyncio.sleep(rng.expovariate(rps))
                task = asyncio.create_task(one(sent))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                sent += 1
                if time.monotonic() - last_report > 30:
                    last_report = time.monotonic()
                    print(summary(stats, latencies, sent), flush=True)
        finally:
            if tasks:
                await asyncio.wait(tasks, timeout=60)
    print(summary(stats, latencies, sent), flush=True)


def summary(stats: dict[str, Counter], latencies: dict[str, list[float]], sent: int) -> str:
    lines = [f"--- sent {sent}"]
    for track in sorted(stats):
        c = stats[track]
        n = sum(c.values())
        lat = sorted(latencies[track])
        p95 = lat[int(0.95 * (len(lat) - 1))] if lat else 0
        ok = c.get("success", 0)
        lines.append(f"{track:>7}: {n:5d} req  success {ok / n:6.1%}  p95 {p95:5.2f}s  {dict(c.most_common())}")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8080")
    p.add_argument("--rps", type=float, default=2.0)
    p.add_argument("--duration", type=float, default=300, help="seconds; 0 = until interrupted")
    p.add_argument("--seed", type=int, default=1)
    args = p.parse_args()
    try:
        asyncio.run(main_async(args.url, args.rps, args.duration, args.seed))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
