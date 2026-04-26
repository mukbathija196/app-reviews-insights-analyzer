"""Google Docs MCP adapter."""

from __future__ import annotations

from typing import Any

from pulse.delivery.mcp_client import MCPClient, as_dict
from pulse.rendering.docops import DocOps, docops_to_dict


class DocsAdapter:
    """Wrap Google Docs tools exposed via MCP."""

    def __init__(self, client: MCPClient) -> None:
        self._client = client

    async def find_or_create_doc(self, title: str) -> str:
        """Return doc_id for the named doc, creating it if absent."""
        raw = await self._client.call_tool("docs_find_or_create_doc", {"title": title})
        data = as_dict(raw)
        return str(data.get("doc_id") or "")

    async def find_heading_anchor(self, doc_id: str, anchor_id: str) -> str | None:
        """Return heading_id if anchor exists, else None (idempotency check)."""
        raw = await self._client.call_tool(
            "docs_find_heading_anchor",
            {"doc_id": doc_id, "anchor_id": anchor_id},
        )
        data = as_dict(raw)
        heading_id = str(data.get("heading_id") or "")
        return heading_id or None

    async def append_section(self, doc_id: str, doc_ops: DocOps) -> str:
        """Apply doc_ops as a new trailing section; return heading_id."""
        raw = await self._client.call_tool(
            "docs_append_section",
            {"doc_id": doc_id, "doc_ops": docops_to_dict(doc_ops)},
        )
        data = as_dict(raw)
        return str(data.get("heading_id") or "")

    async def get_heading_link(self, doc_id: str, heading_id: str) -> str:
        """Return the deep-link URL for a heading."""
        raw = await self._client.call_tool(
            "docs_get_heading_link",
            {"doc_id": doc_id, "heading_id": heading_id},
        )
        data: dict[str, Any] = as_dict(raw)
        return str(data.get("url") or "")


__all__ = ["DocsAdapter"]
