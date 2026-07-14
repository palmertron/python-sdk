"""Toolset Versioning extension (`io.modelcontextprotocol/toolsets`).

Implements the wire shape from the Toolset Versioning SEP: named, SemVer'd,
immutable capability surfaces that clients discover via `toolsets/list` and pin
on `tools/list` / `tools/call`.

A server opts in by passing a `Toolsets` instance to `MCPServer(extensions=[...])`
and registering published Toolset versions with `add_toolset(...)`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp_types import (
    CallToolRequestParams,
    ListToolsetsRequestParams,
    ListToolsetsResult,
    Toolset,
    ToolsetRef,
    ToolsetStatus,
)

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError

EXTENSION_ID = "io.modelcontextprotocol/toolsets"
"""The Toolset Versioning extension identifier."""

TOOLSET_ERROR = -32007
"""Implementation-defined JSON-RPC error code for Toolset pin failures."""


class Toolsets(Extension):
    """Extension that publishes versioned Toolsets and enforces membership pins.

    Register Tools on the host `MCPServer` as usual, then publish Toolset versions
    that name those tools. Passing this extension to `MCPServer` advertises
    `io.modelcontextprotocol/toolsets`, serves `toolsets/list`, and enables
    pin filtering on `tools/list` / `tools/call`.
    """

    identifier = EXTENSION_ID

    def __init__(self) -> None:
        self._toolsets: dict[tuple[str, str], Toolset] = {}

    def add_toolset(
        self,
        *,
        name: str,
        version: str,
        tools: Sequence[str],
        status: ToolsetStatus = "stable",
        title: str | None = None,
        description: str | None = None,
        deprecation_date: str | None = None,
    ) -> Toolset:
        """Publish an immutable Toolset `(name, version)`.

        Args:
            name: Toolset identifier within the server.
            version: Exact SemVer for this publication.
            tools: Ordered membership of tool names.
            status: Lifecycle status (`stable`, `deprecated`, or `experimental`).
            title: Optional human-readable display name.
            description: Optional description of the capability surface.
            deprecation_date: Optional ISO-8601 deprecation date.

        Returns:
            The registered `Toolset`.

        Raises:
            ValueError: If `(name, version)` was already published.
        """
        key = (name, version)
        if key in self._toolsets:
            raise ValueError(f"Toolset {name!r} version {version!r} is already registered")
        toolset = Toolset(
            name=name,
            version=version,
            title=title,
            description=description,
            status=status,
            tools=list(tools),
            deprecation_date=deprecation_date,
        )
        self._toolsets[key] = toolset
        return toolset

    def resolve(self, ref: ToolsetRef) -> Toolset:
        """Return the published Toolset for `ref`, or raise `MCPError`."""
        toolset = self._toolsets.get((ref.name, ref.version))
        if toolset is None:
            raise MCPError(
                code=TOOLSET_ERROR,
                message="Unknown Toolset",
                data={
                    "extension": EXTENSION_ID,
                    "reason": "unknown_toolset",
                    "toolset": {"name": ref.name, "version": ref.version},
                },
            )
        return toolset

    def membership(self, ref: ToolsetRef) -> set[str]:
        """Return the set of tool names in `ref`."""
        return set(self.resolve(ref).tools)

    def ensure_member(self, ref: ToolsetRef, tool_name: str) -> None:
        """Raise `MCPError` if `tool_name` is outside the pinned Toolset."""
        if tool_name not in self.membership(ref):
            raise MCPError(
                code=TOOLSET_ERROR,
                message="Tool not in Toolset",
                data={
                    "extension": EXTENSION_ID,
                    "reason": "tool_not_in_toolset",
                    "toolset": {"name": ref.name, "version": ref.version},
                    "tool": tool_name,
                },
            )

    def list_published(
        self,
        *,
        name: str | None = None,
        status: ToolsetStatus | None = None,
    ) -> list[Toolset]:
        """Return published Toolsets matching optional filters."""
        items = list(self._toolsets.values())
        if name is not None:
            items = [t for t in items if t.name == name]
        if status is not None:
            items = [t for t in items if t.status == status]
        return items

    def methods(self) -> Sequence[MethodBinding]:
        from mcp.server.mcpserver.server import require_client_extension

        extension = self

        async def list_toolsets(
            ctx: ServerRequestContext[Any, Any], params: ListToolsetsRequestParams
        ) -> ListToolsetsResult:
            require_client_extension(ctx, EXTENSION_ID)
            return ListToolsetsResult(toolsets=extension.list_published(name=params.name, status=params.status))

        return [
            MethodBinding(
                "toolsets/list",
                ListToolsetsRequestParams,
                list_toolsets,
            )
        ]

    async def intercept_tool_call(
        self,
        params: CallToolRequestParams,
        ctx: ServerRequestContext[Any, Any],
        call_next: CallNext,
    ) -> HandlerResult:
        if params.toolset is not None:
            from mcp.server.mcpserver.server import require_client_extension

            require_client_extension(ctx, EXTENSION_ID)
            self.ensure_member(params.toolset, params.name)
        return await call_next(ctx)


def toolset_cache_key(toolset: ToolsetRef | None) -> str:
    """Build a response-cache key suffix for a Toolset pin."""
    if toolset is None:
        return ""
    return f"{toolset.name}@{toolset.version}"
