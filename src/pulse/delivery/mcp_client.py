"""Thin MCP host/client wrapper with injectable transport for tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


class MCPStartupError(Exception):
    """Raised when an MCP server subprocess fails to start."""


ToolCaller = Callable[[str, dict[str, object]], Awaitable[object]]


@dataclass
class MCPClient:
    """Routes tool calls through an injected async transport.

    The real MCP wire-up is transport-specific and can vary by server/runtime.
    To keep Phase 5 deterministic and testable, this class accepts an injected
    async ``tool_caller``. Tests provide in-memory fakes; production can provide
    an MCP SDK-backed caller.
    """

    command: str
    transport: str = "stdio"
    tool_caller: ToolCaller | None = None
    started: bool = False

    async def call_tool(self, name: str, args: dict[str, object]) -> object:
        """Invoke a tool through the injected transport."""
        if not self.started:
            raise MCPStartupError("MCP client not started. Use 'async with MCPClient(...)'.")
        if self.tool_caller is None:
            raise MCPStartupError(
                "No MCP tool caller configured. Inject 'tool_caller' in MCPClient."
            )
        return await self.tool_caller(name, args)

    async def __aenter__(self) -> MCPClient:
        if not self.command.strip():
            raise MCPStartupError("MCP command is empty.")
        if self.tool_caller is None and self.transport in {"http", "sse"}:
            self.tool_caller = self._http_call
        self.started = True
        return self

    async def __aexit__(self, *_: object) -> None:
        self.started = False

    async def _http_call(self, name: str, args: dict[str, object]) -> object:
        """Best-effort HTTP/SSE MCP call shapes for hosted MCP gateways."""
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise MCPStartupError("httpx is required for HTTP/SSE MCP transport.") from exc

        base = self.command.rstrip("/")
        simplified = await self._try_saksham_fallback(base, name, args)
        if simplified is not None:
            return simplified

        attempts: list[tuple[str, dict[str, object]]] = [
            (f"{base}/tool/{name}", args),
            (f"{base}/tools/{name}", args),
            (f"{base}/tools/call", {"name": name, "arguments": args}),
            (f"{base}/call_tool", {"name": name, "args": args}),
            (base, {"name": name, "arguments": args}),
        ]
        last_err: Exception | None = None
        async with httpx.AsyncClient(timeout=30.0) as client:
            for url, payload in attempts:
                try:
                    response = await client.post(url, json=payload)
                    if response.status_code >= 400:
                        continue
                    data = response.json()
                    if isinstance(data, dict) and "result" in data:
                        return data["result"]
                    return data
                except Exception as exc:  # pragma: no cover - network dependent
                    last_err = exc
                    continue
        raise MCPStartupError(
            f"Could not call MCP tool '{name}' on endpoint '{base}'. Last error: {last_err}"
        )

    async def _try_saksham_fallback(
        self,
        base: str,
        name: str,
        args: dict[str, object],
    ) -> dict[str, object] | None:
        """Compatibility path for the hosted endpoint with append/draft-only APIs."""
        if "saksham-mcp-server-uht7.onrender.com" not in base:
            return None
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover
            raise MCPStartupError("httpx is required for HTTP/SSE MCP transport.") from exc

        async with httpx.AsyncClient(timeout=30.0) as client:
            if name == "docs_find_or_create_doc":
                title = str(args.get("title") or "").strip()
                if not title:
                    raise MCPStartupError("docs_find_or_create_doc requires a non-empty title.")
                response = await client.post(
                    f"{base}/create_doc",
                    json={"title": title, "content": ""},
                )
                if response.status_code >= 400:
                    raise MCPStartupError(
                        f"create_doc failed with status {response.status_code}: {response.text}"
                    )
                data = response.json()
                doc_id = ""
                if isinstance(data, dict):
                    doc_id = str(
                        data.get("doc_id")
                        or data.get("id")
                        or data.get("document_id")
                        or ""
                    )
                if not doc_id:
                    raise MCPStartupError(
                        "create_doc did not return a doc_id/id in response payload."
                    )
                return {"doc_id": doc_id}
            if name == "docs_find_heading_anchor":
                return {"heading_id": ""}
            if name == "docs_get_heading_link":
                doc_id = str(args.get("doc_id") or "")
                return {"url": f"https://docs.google.com/document/d/{doc_id}/edit"}
            if name == "gmail_search_by_header":
                return {"message_ids": []}
            if name == "docs_append_section":
                doc_id = str(args.get("doc_id") or "")
                content = _doc_ops_to_plain_text(args.get("doc_ops"))
                response = await client.post(
                    f"{base}/append_to_doc",
                    json={"doc_id": doc_id, "content": content},
                )
                if response.status_code >= 400:
                    raise MCPStartupError(
                        f"append_to_doc failed with status {response.status_code}: {response.text}"
                    )
                return {"heading_id": f"append-{doc_id}"}
            if name == "gmail_create_draft":
                recipients = args.get("to")
                to_value = (
                    ",".join(str(x) for x in recipients)
                    if isinstance(recipients, list)
                    else str(recipients or "")
                )
                response = await client.post(
                    f"{base}/create_email_draft",
                    json={
                        "to": to_value,
                        "subject": str(args.get("subject") or ""),
                        "body": str(args.get("text_body") or args.get("html_body") or ""),
                    },
                )
                if response.status_code >= 400:
                    raise MCPStartupError(
                        "create_email_draft failed with status "
                        f"{response.status_code}: {response.text}"
                    )
                data = response.json()
                draft_id = ""
                if isinstance(data, dict):
                    draft_id = str(data.get("draft_id") or data.get("id") or "")
                return {"draft_id": draft_id or "created"}
            if name == "gmail_send":
                recipients = args.get("to")
                to_value = (
                    ",".join(str(x) for x in recipients)
                    if isinstance(recipients, list)
                    else str(recipients or "")
                )
                payload = {
                    "to": to_value,
                    "subject": str(args.get("subject") or ""),
                    "body": str(args.get("text_body") or args.get("html_body") or ""),
                }
                # Hosted MCP variants expose different send endpoints.
                attempts = [
                    (f"{base}/gmail_send", payload),
                    (f"{base}/send_email", payload),
                    (f"{base}/send_email_now", payload),
                    (f"{base}/create_email_draft", {**payload, "send_now": True}),
                ]
                for url, body in attempts:
                    response = await client.post(url, json=body)
                    if response.status_code >= 400:
                        continue
                    data = response.json()
                    message_id = ""
                    if isinstance(data, dict):
                        message_id = str(
                            data.get("message_id")
                            or data.get("sent_id")
                            or data.get("id")
                            or data.get("draft_id")
                            or ""
                        )
                    return {"message_id": message_id or "sent"}
                raise MCPStartupError("Unable to call a compatible send-email endpoint.")
        return None


def _doc_ops_to_plain_text(doc_ops: object) -> str:
    """Convert serialized DocOps payload to clean plain text."""
    if not isinstance(doc_ops, list):
        return str(doc_ops or "")
    blocks: list[str] = []

    def _add_block(text: str) -> None:
        cleaned = text.strip()
        if cleaned:
            blocks.append(cleaned)

    def _format_run_text(run: dict[str, object]) -> str:
        # This MCP endpoint currently accepts plain text only (no rich-text spans).
        return str(run.get("text") or "")

    for item in doc_ops:
        if not isinstance(item, dict):
            continue
        op = str(item.get("op") or "")
        if op == "heading":
            heading = str(item.get("text") or "").strip()
            if heading:
                _add_block(heading)
        elif op == "paragraph":
            runs = item.get("runs")
            if isinstance(runs, list):
                text = "".join(_format_run_text(run) for run in runs if isinstance(run, dict))
                if text.strip():
                    _add_block(text.strip())
        elif op == "bullet_list":
            values = item.get("items")
            if isinstance(values, list):
                items = [f"- {str(value).strip()}" for value in values if str(value).strip()]
                if items:
                    _add_block("\n".join(items))
        elif op == "hr":
            _add_block("---")
    return "\n\n".join(blocks).strip()


def as_dict(payload: object) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    raise TypeError(f"Expected dict payload from MCP server, got: {type(payload)!r}")


__all__ = ["MCPClient", "MCPStartupError", "ToolCaller", "as_dict"]
