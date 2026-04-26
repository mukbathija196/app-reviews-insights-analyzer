"""Phase 5 integration tests — idempotent delivery flow with fakes."""

from __future__ import annotations

import pytest

from pulse.delivery.docs import DocsAdapter
from pulse.delivery.gmail import GmailAdapter
from pulse.delivery.mcp_client import MCPClient
from pulse.delivery.orchestrator import DeliveryOrchestrator
from pulse.rendering.email import RenderedEmail


class FakeMCP:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.anchor_exists = False
        self.sent_ids: list[str] = []
        self.drafts: list[str] = []

    async def call(self, name: str, args: dict[str, object]) -> object:
        self.calls.append(name)
        if name == "docs_find_or_create_doc":
            return {"doc_id": "doc-1"}
        if name == "docs_find_heading_anchor":
            return {"heading_id": "h-1" if self.anchor_exists else None}
        if name == "docs_append_section":
            self.anchor_exists = True
            return {"heading_id": "h-1"}
        if name == "docs_get_heading_link":
            return {"url": "https://docs.google.com/document/d/doc-1/edit#heading=h-1"}
        if name == "gmail_search_by_header":
            return {"message_ids": list(self.sent_ids)}
        if name == "gmail_send":
            self.sent_ids = ["m-1"]
            return {"message_id": "m-1"}
        if name == "gmail_create_draft":
            self.drafts = ["d-1"]
            return {"draft_id": "d-1"}
        return {}


async def _orchestrator(fake: FakeMCP) -> DeliveryOrchestrator:
    client = MCPClient(command="fake", tool_caller=fake.call)
    await client.__aenter__()
    docs = DocsAdapter(client)
    gmail = GmailAdapter(client)
    return DeliveryOrchestrator(docs=docs, gmail=gmail)


@pytest.mark.anyio
async def test_doc_idempotency_hit_second_run_no_append() -> None:
    fake = FakeMCP()
    orchestrator = await _orchestrator(fake)
    email = RenderedEmail(subject="s", html_body="h {{ deep_link }}", text_body="t {{ deep_link }}")

    first = await orchestrator.deliver(
        run_id="r1",
        product="groww",
        iso_week="2026-W16",
        doc_title="Weekly Review Pulse — Groww",
        anchor_id="pulse-groww-2026-W16",
        doc_ops=[],
        email=email,
        email_mode="send",
    )
    second = await orchestrator.deliver(
        run_id="r1",
        product="groww",
        iso_week="2026-W16",
        doc_title="Weekly Review Pulse — Groww",
        anchor_id="pulse-groww-2026-W16",
        doc_ops=[],
        email=email,
        email_mode="send",
    )
    assert first["heading_id"] == "h-1"
    assert second["email_status"] == "already_exists"
    assert fake.calls.count("docs_append_section") == 1
    assert fake.calls.count("gmail_send") == 1


@pytest.mark.anyio
async def test_draft_mode_never_sends() -> None:
    fake = FakeMCP()
    orchestrator = await _orchestrator(fake)
    email = RenderedEmail(subject="s", html_body="h {{ deep_link }}", text_body="t {{ deep_link }}")
    result = await orchestrator.deliver(
        run_id="r2",
        product="groww",
        iso_week="2026-W16",
        doc_title="Weekly Review Pulse — Groww",
        anchor_id="pulse-groww-2026-W16",
        doc_ops=[],
        email=email,
        email_mode="draft",
    )
    assert result["email_status"] == "draft"
    assert "gmail_create_draft" in fake.calls
    assert "gmail_send" not in fake.calls

