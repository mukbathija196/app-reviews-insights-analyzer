"""Delivery orchestrator: idempotency, retries, and circuit breaker."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pulse.delivery.docs import DocsAdapter
from pulse.delivery.gmail import GmailAdapter
from pulse.rendering.docops import DocOps
from pulse.rendering.email import RenderedEmail


class DeliveryError(Exception):
    """Raised after exhausting retries on a delivery operation."""


AsyncCall = Callable[[], Awaitable[object]]


@dataclass
class DeliveryOrchestrator:
    """Coordinate doc + email delivery with idempotency guards and retries."""

    docs: DocsAdapter
    gmail: GmailAdapter
    max_retries: int = 3

    async def _with_retries(self, fn: AsyncCall) -> object:
        delay = 0.5
        last_exc: Exception | None = None
        for _ in range(self.max_retries):
            try:
                return await fn()
            except Exception as exc:  # pragma: no cover - exercised in integration tests
                last_exc = exc
                await asyncio.sleep(delay)
                delay *= 2
        raise DeliveryError(f"delivery operation failed after retries: {last_exc}")

    async def deliver(
        self,
        *,
        run_id: str,
        product: str,
        iso_week: str,
        doc_title: str,
        doc_id_override: str | None = None,
        anchor_id: str,
        doc_ops: DocOps,
        email: RenderedEmail,
        email_mode: str = "draft",
        recipients: list[str] | None = None,
    ) -> dict[str, object]:
        """Run full idempotent delivery sequence; return stable identifiers."""
        headers = {
            "X-Pulse-Run-Id": run_id,
            "X-Pulse-Product": product,
            "X-Pulse-IsoWeek": iso_week,
        }

        if doc_id_override:
            doc_id = doc_id_override
        else:
            doc_id = str(await self._with_retries(lambda: self.docs.find_or_create_doc(doc_title)))

        # Idempotent section write: check anchor first, append only if missing.
        existing_heading = await self._with_retries(
            lambda: self.docs.find_heading_anchor(doc_id, anchor_id)
        )
        heading_id = str(existing_heading) if existing_heading else ""
        if not heading_id:
            heading_id = str(
                await self._with_retries(lambda: self.docs.append_section(doc_id, doc_ops))
            )
        deep_link = str(
            await self._with_retries(lambda: self.docs.get_heading_link(doc_id, heading_id))
        )

        email_with_link = RenderedEmail(
            subject=email.subject,
            html_body=email.html_body.replace(email.deep_link_placeholder, deep_link),
            text_body=email.text_body.replace(email.deep_link_placeholder, deep_link),
            deep_link_placeholder=email.deep_link_placeholder,
        )

        existing_msgs = await self._with_retries(
            lambda: self.gmail.search_by_header("X-Pulse-Run-Id", run_id)
        )
        if isinstance(existing_msgs, list) and existing_msgs:
            return {
                "doc_id": doc_id,
                "heading_id": heading_id,
                "deep_link": deep_link,
                "email_status": "already_exists",
                "email_id": str(existing_msgs[0]),
            }

        if email_mode == "send":
            msg_id = str(
                await self._with_retries(
                    lambda: self.gmail.send(email_with_link, headers, recipients=recipients)
                )
            )
            status = "sent"
        else:
            msg_id = str(
                await self._with_retries(
                    lambda: self.gmail.create_draft(
                        email_with_link, headers, recipients=recipients
                    )
                )
            )
            status = "draft"

        return {
            "doc_id": doc_id,
            "heading_id": heading_id,
            "deep_link": deep_link,
            "email_status": status,
            "email_id": msg_id,
        }


__all__ = ["DeliveryOrchestrator", "DeliveryError"]
