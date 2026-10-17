from agent.guardrails import Budget, LoopDetector


def test_identical_calls_trip_on_third_repeat():
    d = LoopDetector(max_identical=3)
    assert d.observe("get_order", {"order_id": "1"}) is None
    assert d.observe("get_order", {"order_id": "1"}) is None
    trip = d.observe("get_order", {"order_id": "1"})
    assert trip and trip.guardrail == "loop_detected"


def test_argument_order_does_not_hide_a_loop():
    d = LoopDetector(max_identical=2)
    d.observe("search_kb", {"query": "x", "top_k": 2})
    assert d.observe("search_kb", {"top_k": 2, "query": "x"}) is not None


def test_same_tool_different_arguments_is_not_a_loop():
    d = LoopDetector(max_identical=3)
    for oid in ["1", "2", "3", "4", "5"]:
        assert d.observe("get_order", {"order_id": oid}) is None


def test_two_step_cycle_is_detected():
    d = LoopDetector(max_identical=10)
    assert d.observe("get_order", {"order_id": "1"}) is None
    assert d.observe("search_kb", {"query": "returns"}) is None
    assert d.observe("get_order", {"order_id": "1"}) is None
    trip = d.observe("search_kb", {"query": "returns"})
    assert trip and "cycle of 2" in trip.detail


def test_step_budget():
    b = Budget(max_steps=2, max_tokens=10_000, max_cost_usd=1.0, timeout_s=60)
    assert b.next_step() is None
    assert b.next_step() is None
    trip = b.next_step()
    assert trip and trip.guardrail == "max_steps"


def test_token_and_cost_budgets():
    b = Budget(max_steps=10, max_tokens=1000, max_cost_usd=0.01, timeout_s=60)
    assert b.charge(500, 0.001) is None
    assert b.charge(600, 0.001).guardrail == "token_budget"
    b = Budget(max_steps=10, max_tokens=10**6, max_cost_usd=0.01, timeout_s=60)
    assert b.charge(10, 0.02).guardrail == "cost_budget"


def test_timeout_budget():
    b = Budget(max_steps=10, max_tokens=10**6, max_cost_usd=1, timeout_s=0)
    trip = b.next_step()
    assert trip and trip.guardrail == "timeout"
