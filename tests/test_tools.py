import asyncio

from mcp_server.server import TOOL_NAMES, mcp


def test_resolve_people_is_advertised_first():
    tools = asyncio.run(mcp.list_tools())
    names = [t.name for t in tools]
    assert names[0] == "resolve_people"
    assert set(names) == set(TOOL_NAMES)
    resolve = next(t for t in tools if t.name == "resolve_people")
    schema = resolve.input_schema or {}
    props = schema.get("properties") or {}
    assert "source_table" in props
    assert "client_tag" in props
    assert "min_tier" in props
    assert "skip_tiers" in props
    cost = props["approve_cost_usd"]
    assert cost.get("type") == "number"
    assert "anyOf" not in cost
    anns = resolve.annotations
    hint = None
    if anns is not None:
        hint = getattr(anns, "destructive_hint", getattr(anns, "destructiveHint", None))
    assert hint is not True
