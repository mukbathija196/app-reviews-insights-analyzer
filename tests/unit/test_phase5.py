"""Phase 5 unit tests — MCP client, adapters, and orchestration order."""

from __future__ import annotations

import pytest

from pulse.delivery.docs import DocsAdapter
from pulse.delivery.gmail import GmailAdapter
from pulse.delivery.mcp_client import MCPClient, MCPStartupError
from pulse.delivery.orchestrator import DeliveryOrchestrator
from pulse.rendering.email import RenderedEmail


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def call(self, name: str, args: dict[str, object]) -> object:
        self.calls.append((name, args))
        if name == "docs_find_or_create_doc":
            return {"doc_id": "doc-1"}
        if name == "docs_find_heading_anchor":
            return {"heading_id": None}
        if name == "docs_append_section":
            return {"heading_id": "h-1"}
        if name == "docs_get_heading_link":
            return {"url": "https://docs.google.com/document/d/doc-1/edit#heading=h-1"}
        if name == "gmail_search_by_header":
            return {"message_ids": []}
        if name == "gmail_create_draft":
            return {"draft_id": "d-1"}
        if name == "gmail_send":
            return {"message_id": "m-1"}
        return {}


@pytest.mark.anyio
async def test_mcp_client_dispatches_tool_call() -> None:
    rec = Recorder()
    async with MCPClient(command="fake", tool_caller=rec.call) as client:
        out = await client.call_tool("docs_find_or_create_doc", {"title": "x"})
    assert out == {"doc_id": "doc-1"}
    assert rec.calls[0][0] == "docs_find_or_create_doc"


@pytest.mark.anyio
async def test_mcp_client_requires_context_manager() -> None:
    rec = Recorder()
    client = MCPClient(command="fake", tool_caller=rec.call)
    try:
        await client.call_tool("any", {})
        raise AssertionError("should have raised")
    except MCPStartupError:
        assert True


@pytest.mark.anyio
async def test_docs_adapter_calls_find_first() -> None:
    rec = Recorder()
    async with MCPClient(command="fake", tool_caller=rec.call) as client:
        docs = DocsAdapter(client)
        _ = await docs.find_or_create_doc("Groww")
        _ = await docs.find_heading_anchor("doc-1", "pulse-groww-2026-W16")
    names = [name for name, _ in rec.calls]
    assert names[:2] == ["docs_find_or_create_doc", "docs_find_heading_anchor"]


@pytest.mark.anyio
async def test_gmail_search_before_send() -> None:
    rec = Recorder()
    async with MCPClient(command="fake", tool_caller=rec.call) as client:
        docs = DocsAdapter(client)
        gmail = GmailAdapter(client)
        orchestrator = DeliveryOrchestrator(docs=docs, gmail=gmail)
        email = RenderedEmail(
            subject="s",
            html_body="go {{ deep_link }}",
            text_body="go {{ deep_link }}",
        )
        _ = await orchestrator.deliver(
            run_id="r1",
            product="groww",
            iso_week="2026-W16",
            doc_title="Weekly Review Pulse — Groww",
            anchor_id="pulse-groww-2026-W16",
            doc_ops=[],
            email=email,
            email_mode="send",
        )
    names = [name for name, _ in rec.calls]
    assert "gmail_search_by_header" in names
    assert names.index("gmail_search_by_header") < names.index("gmail_send")

