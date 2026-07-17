# Toolsets

**Toolsets** are named, semantically versioned, immutable capability surfaces: a fixed
membership of tool names that a client can discover and pin on `tools/list` and
`tools/call`. They address uncontrolled tool-surface expansion when agents discover
tools dynamically — a client pinned to `core-ops@1.2.0` never sees tools that only
exist in a later Toolset version.

This page documents the SDK's **draft reference** implementation of the Toolset
Versioning SEP (`Toolsets` / `io.modelcontextprotocol/toolsets`). The surface is
opt-in and may change while the SEP is under review — treat it as incubating, not a
frozen stable API. If [Extensions](extensions.md) are new to you, skim that page first.

## Advertise and publish

```python
from mcp.server.mcpserver import MCPServer
from mcp.server.toolsets import Toolsets

toolsets = Toolsets()
mcp = MCPServer("crm", extensions=[toolsets])

@mcp.tool()
def search_contacts(query: str) -> str:
    return f"found:{query}"

@mcp.tool()
def analyze_report(report_id: str) -> str:
    return f"report:{report_id}"

toolsets.add_toolset(
    name="core-ops",
    version="1.2.0",
    status="stable",
    tools=["search_contacts"],
)
toolsets.add_toolset(
    name="core-ops",
    version="1.3.0",
    status="stable",
    tools=["search_contacts", "analyze_report"],
)
```

The SDK rejects republishing the same `(name, version)` in-process. Across
deployments, permanent identity non-reuse and wire-contract stability are
publisher conformance requirements from the SEP, not mechanically enforced
tombstones. Register ordinary tools with `@mcp.tool()`; Toolsets only declare
membership by name.

## Client pin

Clients that pin Toolsets must advertise the extension, then pass the same
`ToolsetRef` on list and call:

```python
from mcp import Client
from mcp.client import advertise
from mcp.server.mcpserver import MCPServer
from mcp.server.toolsets import EXTENSION_ID
from mcp_types import ToolsetRef

pin = ToolsetRef(name="core-ops", version="1.2.0")


async def use_toolset(mcp: MCPServer[object]) -> None:
    async with Client(mcp, extensions=[advertise(EXTENSION_ID)]) as client:
        published = await client.list_toolsets()
        tools = await client.list_tools(toolset=pin)  # no analyze_report
        result = await client.call_tool("search_contacts", {"query": "acme"}, toolset=pin)
```

`toolsets/list` is paginated. Pass each opaque cursor back unchanged; the cursor
continues the original filtered query, so continuation requests may omit the filters:

```python
from mcp import Client


async def list_stable_toolsets(client: Client) -> None:
    cursor = None
    while True:
        page = await client.list_toolsets(
            name="core-ops" if cursor is None else None,
            status="stable" if cursor is None else None,
            cursor=cursor,
        )
        for published_toolset in page.toolsets:
            print(published_toolset.name, published_toolset.version)
        cursor = page.next_cursor
        if cursor is None:
            break
```

Omitting `toolset` keeps today's full flat catalog. Calling a non-member under a
pin returns a protocol `MCPError` (`reason: tool_not_in_toolset`), not a tool
`is_error` result. Membership names with no registered tool are omitted from a
pinned `tools/list` (the list does not fail).

If a server later returns multi-page pinned `tools/list` results, membership
filtering applies **before** paging. This SDK currently returns the complete
filtered membership as a single page.

## Cache keys

Pinned `tools/list` responses are cached separately from the unpinned catalog
(and from other pins). Distinct pins never reuse an unpinned cache entry.
