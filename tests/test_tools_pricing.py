import pytest

from agent import tools
from agent.pricing import Usage, cost_usd, is_priced


def test_get_order_found_and_missing():
    assert tools.get_order("#10232")["status"] == "shipped"
    with pytest.raises(tools.ToolError):
        tools.get_order("99999")


def test_search_kb_ranks_title_matches_first():
    res = tools.search_kb("refund timing")["results"]
    assert res[0]["id"] == "kb-refunds"


def test_calculate_is_safe():
    assert tools.calculate("179-5.95")["result"] == 173.05
    for bad in ["__import__('os')", "2**1000", "a+1", "1/0"]:
        with pytest.raises(tools.ToolError):
            tools.calculate(bad)


def test_create_ticket_validates_priority():
    assert tools.create_ticket("help", "high")["status"] == "open"
    with pytest.raises(tools.ToolError):
        tools.create_ticket("help", "urgent")


def test_tool_specs_are_strict_and_sorted():
    specs = tools.tool_specs()
    assert [s["name"] for s in specs] == sorted(s["name"] for s in specs)
    for s in specs:
        assert s["strict"] is True
        assert s["input_schema"]["additionalProperties"] is False


def test_unknown_tool_and_bad_args_are_tool_errors():
    with pytest.raises(tools.ToolError):
        tools.run_tool("nope", {})
    with pytest.raises(tools.ToolError):
        tools.run_tool("get_order", {"wrong": "x"})


def test_cost_includes_cache_tokens():
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000, cache_write_tokens=0)
    # Opus 5.5: $4 in + $20 out + $0.20 cache read
    assert cost_usd("claude-opus-5-5", u) == pytest.approx(24.20)
    assert cost_usd("claude-opus-5-5", Usage(cache_write_tokens=1_000_000)) == pytest.approx(5.0)


def test_unknown_model_costs_zero_and_is_flagged():
    assert cost_usd("some-new-model", Usage(1000, 1000)) == 0.0
    assert not is_priced("some-new-model")
