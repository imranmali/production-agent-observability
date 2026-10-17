from canary.analysis import TrackMetrics, evaluate, load_policy

POLICY = load_policy()


def m(requests=200, failures=2, loops=0, p95=3.0, cost=1.0) -> TrackMetrics:
    return TrackMetrics(requests=requests, failures=failures, loops=loops, p95_latency_s=p95, cost_usd=cost)


def test_healthy_canary_passes():
    v = evaluate(m(requests=60, failures=1, cost=0.3), m(), POLICY)
    assert v.status == "pass", v.summary()


def test_too_little_traffic_is_inconclusive():
    v = evaluate(m(requests=5, failures=0), m(), POLICY)
    assert v.status == "inconclusive"


def test_loops_fail_the_canary():
    v = evaluate(m(requests=100, failures=3, loops=3, cost=0.5), m(), POLICY)
    assert v.status == "fail" and "loop ratio" in v.reason


def test_failure_ratio_is_relative_to_stable():
    # Stable 1%, canary 4%: inside the 5% ceiling but more than 2pp worse.
    v = evaluate(m(requests=100, failures=4, cost=0.5), m(requests=200, failures=2), POLICY)
    assert v.status == "fail" and "failure ratio" in v.reason


def test_perfect_stable_does_not_demand_perfect_canary():
    v = evaluate(m(requests=100, failures=1, cost=0.5), m(requests=200, failures=0), POLICY)
    assert v.status == "pass", v.summary()


def test_latency_regression_fails():
    v = evaluate(m(requests=100, failures=0, p95=6.0, cost=0.5), m(p95=3.0), POLICY)
    assert v.status == "fail" and "p95 latency" in v.reason


def test_small_latency_differences_are_ignored():
    v = evaluate(m(requests=100, failures=0, p95=0.9, cost=0.5), m(p95=0.6), POLICY)
    assert v.status == "pass", v.summary()


def test_cost_regression_fails():
    # Stable $0.005/request, canary $0.01/request.
    v = evaluate(m(requests=100, failures=0, cost=1.0), m(requests=200, cost=1.0), POLICY)
    assert v.status == "fail" and "cost per request" in v.reason


def test_firing_critical_alert_fails_immediately():
    v = evaluate(m(), m(), POLICY, firing_critical=["AgentLoopRateHigh"])
    assert v.status == "fail" and "alert" in v.reason
