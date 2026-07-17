"""In-memory tests for the Toolset Versioning SEP draft reference implementation."""

from __future__ import annotations

import pytest
from mcp_types import (
    METHOD_NOT_FOUND,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    TextContent,
    ToolsetRef,
)

from mcp import Client, MCPError
from mcp.client import advertise
from mcp.server.mcpserver import MCPServer
from mcp.server.toolsets import EXTENSION_ID, TOOLSET_ERROR, Toolsets, toolset_cache_key

pytestmark = pytest.mark.anyio


def _crm_server() -> tuple[MCPServer[object], Toolsets]:
    toolsets = Toolsets()
    mcp = MCPServer("crm", extensions=[toolsets])

    @mcp.tool()
    def search_contacts(query: str) -> str:
        return f"found:{query}"

    @mcp.tool()
    def create_deal(title: str) -> str:
        return f"deal:{title}"

    @mcp.tool()
    def update_deal_stage(deal_id: str, stage: str) -> str:
        return f"{deal_id}:{stage}"

    @mcp.tool()
    def analyze_report(report_id: str) -> str:
        return f"report:{report_id}"

    toolsets.add_toolset(
        name="core-ops",
        version="1.2.0",
        status="stable",
        tools=["search_contacts", "create_deal", "update_deal_stage"],
    )
    toolsets.add_toolset(
        name="core-ops",
        version="1.3.0",
        status="stable",
        tools=["search_contacts", "create_deal", "update_deal_stage", "analyze_report"],
    )
    return mcp, toolsets


async def test_toolsets_extension_is_advertised_in_server_capabilities() -> None:
    """Spec/SDK: Toolsets advertises under capabilities.extensions when installed."""
    mcp, _ = _crm_server()
    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        assert client.protocol_version == "2026-07-28"
        assert client.server_capabilities.extensions is not None
        assert EXTENSION_ID in client.server_capabilities.extensions


async def test_toolsets_list_returns_method_not_found_for_legacy_client() -> None:
    """Spec: the Toolsets extension does not define support for legacy protocol revisions."""
    mcp, _ = _crm_server()
    async with Client(mcp, mode="legacy", extensions=[advertise(EXTENSION_ID)]) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.session.list_toolsets()
        assert exc_info.value.code == METHOD_NOT_FOUND


async def test_toolsets_list_returns_published_toolsets_and_filters_by_name() -> None:
    """Spec: toolsets/list returns published Toolsets; name filter narrows results."""
    mcp, _ = _crm_server()
    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        all_sets = await client.list_toolsets()
        assert {(t.name, t.version) for t in all_sets.toolsets} == {
            ("core-ops", "1.2.0"),
            ("core-ops", "1.3.0"),
        }
        named = await client.list_toolsets(name="core-ops")
        assert len(named.toolsets) == 2
        empty = await client.list_toolsets(name="missing")
        assert empty.toolsets == []


async def test_unpinned_tools_list_returns_full_catalog() -> None:
    """Spec: omitting toolset preserves today's full flat tools/list."""
    mcp, _ = _crm_server()
    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        result = await client.list_tools()
        assert {t.name for t in result.tools} == {
            "search_contacts",
            "create_deal",
            "update_deal_stage",
            "analyze_report",
        }


async def test_pinned_tools_list_returns_registered_members() -> None:
    """Spec: tools/list with a pin returns registered members of the Toolset."""
    mcp, _ = _crm_server()
    pin = ToolsetRef(name="core-ops", version="1.2.0")
    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        result = await client.list_tools(toolset=pin)
        assert [t.name for t in result.tools] == [
            "search_contacts",
            "create_deal",
            "update_deal_stage",
        ]


async def test_unknown_toolset_pin_on_list_returns_unknown_toolset_error() -> None:
    """Spec: unknown (name, version) on tools/list returns unknown_toolset."""
    mcp, _ = _crm_server()
    pin = ToolsetRef(name="core-ops", version="9.9.9")
    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.list_tools(toolset=pin)
        assert exc_info.value.code == TOOLSET_ERROR
        assert exc_info.value.data["reason"] == "unknown_toolset"


async def test_unknown_toolset_pin_on_call_returns_unknown_toolset_error() -> None:
    """Spec: unknown (name, version) on tools/call returns unknown_toolset."""
    mcp, _ = _crm_server()
    pin = ToolsetRef(name="core-ops", version="9.9.9")
    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("search_contacts", {"query": "acme"}, toolset=pin)
        assert exc_info.value.code == TOOLSET_ERROR
        assert exc_info.value.data["reason"] == "unknown_toolset"


async def test_pinned_tools_list_omits_membership_names_without_registered_tools() -> None:
    """Spec: pinned tools/list omits membership names that have no registered tool."""
    toolsets = Toolsets()
    mcp = MCPServer("crm", extensions=[toolsets])

    @mcp.tool()
    def search_contacts(query: str) -> str:
        return f"found:{query}"

    toolsets.add_toolset(
        name="core-ops",
        version="1.0.0",
        status="stable",
        tools=["search_contacts", "ghost_tool_not_registered"],
    )
    pin = ToolsetRef(name="core-ops", version="1.0.0")

    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        result = await client.list_tools(toolset=pin)
        assert [t.name for t in result.tools] == ["search_contacts"]


async def test_pinned_call_rejects_non_member_and_allows_member() -> None:
    """Spec: tools/call under a pin rejects non-members and runs members."""
    mcp, _ = _crm_server()
    pin = ToolsetRef(name="core-ops", version="1.2.0")
    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("analyze_report", {"report_id": "r1"}, toolset=pin)
        assert exc_info.value.code == TOOLSET_ERROR
        assert exc_info.value.data["reason"] == "tool_not_in_toolset"

        ok = await client.call_tool("search_contacts", {"query": "acme"}, toolset=pin)
        assert ok.is_error is False
        assert isinstance(ok.content[0], TextContent)
        assert ok.content[0].text == "found:acme"


async def test_older_toolset_pin_does_not_see_tools_added_in_newer_version() -> None:
    """Spec: concurrent Toolset versions keep immutable membership per pin."""
    mcp, _ = _crm_server()
    pin_old = ToolsetRef(name="core-ops", version="1.2.0")
    pin_new = ToolsetRef(name="core-ops", version="1.3.0")
    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        old_names = {t.name for t in (await client.list_tools(toolset=pin_old)).tools}
        new_names = {t.name for t in (await client.list_tools(toolset=pin_new)).tools}
        assert "analyze_report" not in old_names
        assert "analyze_report" in new_names


async def test_client_without_toolsets_advertisement_can_use_unpinned_tools() -> None:
    """Spec: extension is optional; unpinned clients keep basic tool use."""
    mcp, _ = _crm_server()
    async with Client(mcp, mode="auto") as client:
        result = await client.list_tools()
        assert len(result.tools) == 4
        called = await client.call_tool("search_contacts", {"query": "x"})
        assert isinstance(called.content[0], TextContent)
        assert called.content[0].text == "found:x"


async def test_toolsets_list_without_client_advertisement_is_rejected() -> None:
    """Spec: toolsets/list requires per-request client extension advertisement."""
    mcp, _ = _crm_server()
    async with Client(mcp, mode="auto") as client:
        with pytest.raises(MCPError) as exc_info:
            await client.list_toolsets()
        assert exc_info.value.code == MISSING_REQUIRED_CLIENT_CAPABILITY


async def test_pinned_tools_list_without_client_advertisement_is_rejected() -> None:
    """Spec: a pinned tools/list requires per-request client extension advertisement."""
    mcp, _ = _crm_server()
    pin = ToolsetRef(name="core-ops", version="1.2.0")
    async with Client(mcp, mode="auto") as client:
        with pytest.raises(MCPError) as exc_info:
            await client.list_tools(toolset=pin)
        assert exc_info.value.code == MISSING_REQUIRED_CLIENT_CAPABILITY


async def test_pinned_tool_call_without_client_advertisement_is_rejected() -> None:
    """Spec: a pinned tools/call requires per-request client extension advertisement."""
    mcp, _ = _crm_server()
    pin = ToolsetRef(name="core-ops", version="1.2.0")
    async with Client(mcp, mode="auto") as client:
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("search_contacts", {"query": "acme"}, toolset=pin)
        assert exc_info.value.code == MISSING_REQUIRED_CLIENT_CAPABILITY


async def test_toolsets_list_rejects_server_without_extension_advertisement() -> None:
    """Spec: clients confirm server support before sending toolsets/list."""
    mcp = MCPServer("crm")
    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.list_toolsets()
        assert exc_info.value.code == METHOD_NOT_FOUND
        assert exc_info.value.data == {"extension": EXTENSION_ID}


async def test_pinned_tools_list_rejects_server_without_extension_advertisement() -> None:
    """Spec: clients confirm server support before sending a pinned tools/list."""
    mcp = MCPServer("crm")
    pin = ToolsetRef(name="core-ops", version="1.2.0")
    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.list_tools(toolset=pin)
        assert exc_info.value.code == METHOD_NOT_FOUND
        assert exc_info.value.data == {"extension": EXTENSION_ID}


async def test_pinned_tool_call_rejects_server_without_extension_advertisement() -> None:
    """Spec: clients confirm server support before sending a pinned tools/call."""
    mcp = MCPServer("crm")
    pin = ToolsetRef(name="core-ops", version="1.2.0")
    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("search_contacts", {"query": "acme"}, toolset=pin)
        assert exc_info.value.code == METHOD_NOT_FOUND
        assert exc_info.value.data == {"extension": EXTENSION_ID}


async def test_toolsets_list_filters_by_status() -> None:
    """Spec: toolsets/list status filter returns only matching lifecycle values."""
    toolsets = Toolsets()
    mcp = MCPServer("crm", extensions=[toolsets])

    @mcp.tool()
    def search_contacts(query: str) -> str:
        return query

    toolsets.add_toolset(name="core-ops", version="1.0.0", status="stable", tools=["search_contacts"])
    toolsets.add_toolset(name="core-ops", version="2.0.0-exp", status="experimental", tools=["search_contacts"])

    async with Client(mcp, mode="auto", extensions=[advertise(EXTENSION_ID)]) as client:
        stable = await client.list_toolsets(status="stable")
        assert [(t.name, t.version) for t in stable.toolsets] == [("core-ops", "1.0.0")]


def test_toolset_cache_key_encodes_pin_or_empty() -> None:
    """SDK: cache key is empty when unpinned and name@version when pinned."""
    assert toolset_cache_key(None) == ""
    assert toolset_cache_key(ToolsetRef(name="core-ops", version="1.2.0")) == "core-ops@1.2.0"


async def test_duplicate_toolset_registration_raises() -> None:
    """SDK: republishing the same (name, version) is rejected."""
    toolsets = Toolsets()
    toolsets.add_toolset(name="core-ops", version="1.0.0", tools=["a"])
    with pytest.raises(ValueError):
        toolsets.add_toolset(name="core-ops", version="1.0.0", tools=["b"])
