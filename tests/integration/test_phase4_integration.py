"""Phase 4 integration tests — DocOps roundtrip and HTML parse sanity."""

from __future__ import annotations

from html.parser import HTMLParser

from pulse.reasoning.theme import ActionIdea, AudienceHelp, Quote, Theme
from pulse.rendering.docops import docops_from_dict, docops_to_dict, render_docops
from pulse.rendering.email import render_email
from pulse.rendering.report import Report
from pulse.run import RunSpec


class _TagBalanceParser(HTMLParser):
    """Minimal unclosed-tag detector for snapshot-generated HTML."""

    VOID = {"br", "img", "hr", "meta", "link", "input"}

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        if tag in self.VOID:
            return
        if not self.stack:
            raise AssertionError(f"Unexpected closing tag: {tag}")
        last = self.stack.pop()
        if last != tag:
            raise AssertionError(f"Mismatched closing tag: expected {last}, got {tag}")


def _report() -> Report:
    spec = RunSpec(product="groww", iso_week="2026-W16")
    theme = Theme(
        theme_name="Charges spike after updates",
        one_liner="Users report sudden fee surprises after recent app updates.",
        leadership_summary="Unexpected charges are eroding trust and driving churn risk.",
        severity="high",
        confidence="medium",
        quotes=[Quote(review_id="play_store:groww:1", text="charges became very high")],
        action_ideas=[
            ActionIdea(
                title="Improve fee transparency",
                rationale="Users need charge breakdown before order confirm.",
                impact="Lowers billing complaints and improves trust.",
            )
        ],
        who_this_helps=[
            AudienceHelp(
                audience="leadership",
                why="Clarifies trust risk and urgency for pricing communication changes.",
            )
        ],
        cluster_id=9,
        n_reviews=21,
    )
    return Report(spec=spec, themes=[theme], window_label="Last 12 weeks")


def test_docops_serialization_roundtrip() -> None:
    payload = docops_to_dict(render_docops(_report()))
    restored = docops_from_dict(payload)
    assert docops_to_dict(restored) == payload


def test_rendered_html_has_no_unclosed_tags() -> None:
    rendered = render_email(_report())
    parser = _TagBalanceParser()
    parser.feed(rendered.html_body)
    assert parser.stack == []

