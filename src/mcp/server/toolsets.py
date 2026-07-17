"""Toolset Versioning extension (`io.modelcontextprotocol/toolsets`).

Draft reference implementation of the Toolset Versioning SEP: named, semantically
versioned capability surfaces that clients discover via paginated `toolsets/list`
and pin on `tools/list` / `tools/call`.

Membership pins are enforced mechanically. Cross-version wire-contract stability
and permanent `(name, version)` non-reuse are publisher conformance requirements.

A server opts in by passing a `Toolsets` instance to `MCPServer(extensions=[...])`
and registering published Toolset versions with `add_toolset(...)`.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Sequence
from typing import Any, Literal

from mcp_types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    ListToolsetsRequestParams,
    ListToolsetsResult,
    Toolset,
    ToolsetRef,
    ToolsetStatus,
)
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, ValidationError

from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.shared.exceptions import MCPError

EXTENSION_ID = "io.modelcontextprotocol/toolsets"
"""The Toolset Versioning extension identifier."""

TOOLSET_ERROR = -32007
"""Implementation-defined JSON-RPC error code for Toolset pin failures."""


class _ToolsetsCursor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, strict=True)

    format_version: Literal[1] = Field(default=1, alias="v")
    offset: NonNegativeInt = Field(alias="o")
    name: str | None = Field(alias="n")
    status: ToolsetStatus | None = Field(alias="s")


def _invalid_cursor() -> MCPError:
    return MCPError(
        code=INVALID_PARAMS,
        message="Invalid toolsets/list cursor",
        data={"reason": "invalid_cursor"},
    )


def _encode_cursor(cursor: _ToolsetsCursor) -> str:
    payload = cursor.model_dump_json(by_alias=True).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def _decode_cursor(value: str) -> _ToolsetsCursor:
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        return _ToolsetsCursor.model_validate_json(raw)
    except (binascii.Error, ValidationError):
        raise _invalid_cursor() from None


class Toolsets(Extension):
    """2026-07-28+ extension that publishes Toolsets and enforces membership pins.

    Register Tools on the host `MCPServer` as usual, then publish Toolset versions
    that name those tools. Passing this extension to `MCPServer` advertises
    `io.modelcontextprotocol/toolsets`, serves `toolsets/list`, and enables
    pin filtering on `tools/list` / `tools/call`.
    """

    identifier = EXTENSION_ID

    def __init__(self, *, page_size: int = 100) -> None:
        """Create a Toolsets extension with a server-selected page size.

        Args:
            page_size: Maximum publications returned by one `toolsets/list` page.

        Raises:
            ValueError: If `page_size` is not positive.
        """
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        self._page_size = page_size
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
        """Publish a Toolset `(name, version)`.

        The SDK rejects duplicate registration in this process. Permanent
        non-reuse after retirement and member wire-contract stability remain
        publisher conformance requirements.

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

    def _list_page(
        self,
        *,
        name: str | None,
        status: ToolsetStatus | None,
        cursor: str | None,
    ) -> ListToolsetsResult:
        offset = 0
        if cursor is not None:
            decoded = _decode_cursor(cursor)
            if (name is not None and name != decoded.name) or (status is not None and status != decoded.status):
                raise _invalid_cursor()
            name = decoded.name
            status = decoded.status
            offset = decoded.offset

        items = self.list_published(name=name, status=status)
        if cursor is not None and offset >= len(items):
            raise _invalid_cursor()
        end = min(offset + self._page_size, len(items))
        next_cursor = _encode_cursor(_ToolsetsCursor(o=end, n=name, s=status)) if end < len(items) else None
        return ListToolsetsResult(toolsets=items[offset:end], next_cursor=next_cursor)

    def methods(self) -> Sequence[MethodBinding]:
        from mcp.server.mcpserver.server import require_client_extension

        extension = self

        async def list_toolsets(
            ctx: ServerRequestContext[Any, Any], params: ListToolsetsRequestParams
        ) -> ListToolsetsResult:
            require_client_extension(ctx, EXTENSION_ID)
            return extension._list_page(name=params.name, status=params.status, cursor=params.cursor)

        return [
            MethodBinding(
                "toolsets/list",
                ListToolsetsRequestParams,
                list_toolsets,
                protocol_versions=frozenset(MODERN_PROTOCOL_VERSIONS),
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
