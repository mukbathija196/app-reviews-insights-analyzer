"""Gmail MCP adapter."""

from __future__ import annotations

from pulse.delivery.mcp_client import MCPClient, as_dict
from pulse.rendering.email import RenderedEmail


class GmailAdapter:
    """Wrap Gmail tools exposed via MCP."""

    def __init__(self, client: MCPClient) -> None:
        self._client = client

    async def search_by_header(self, header_name: str, header_value: str) -> list[str]:
        """Return message IDs matching the custom header (idempotency check)."""
        raw = await self._client.call_tool(
            "gmail_search_by_header",
            {"header_name": header_name, "header_value": header_value},
        )
        data = as_dict(raw)
        items = data.get("message_ids") or []
        if not isinstance(items, list):
            return []
        return [str(x) for x in items]

    async def create_draft(
        self,
        email: RenderedEmail,
        custom_headers: dict[str, str],
        recipients: list[str] | None = None,
    ) -> str:
        """Create a Gmail draft and return draft_id."""
        raw = await self._client.call_tool(
            "gmail_create_draft",
            {
                "subject": email.subject,
                "html_body": email.html_body,
                "text_body": email.text_body,
                "to": recipients or [],
                "headers": custom_headers,
            },
        )
        data = as_dict(raw)
        return str(data.get("draft_id") or "")

    async def send(
        self,
        email: RenderedEmail,
        custom_headers: dict[str, str],
        recipients: list[str] | None = None,
    ) -> str:
        """Send an email and return message_id."""
        raw = await self._client.call_tool(
            "gmail_send",
            {
                "subject": email.subject,
                "html_body": email.html_body,
                "text_body": email.text_body,
                "to": recipients or [],
                "headers": custom_headers,
            },
        )
        data = as_dict(raw)
        return str(data.get("message_id") or "")


__all__ = ["GmailAdapter"]
