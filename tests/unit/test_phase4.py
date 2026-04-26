"""Phase 4 unit tests — deterministic DocOps and email rendering snapshots."""

from __future__ import annotations

import json
from pathlib import Path

from pulse.reasoning.theme import ActionIdea, AudienceHelp, Quote, Theme
from pulse.rendering.docops import docops_to_dict, render_docops
from pulse.rendering.email import render_email
from pulse.rendering.report import Report
from pulse.run import RunSpec

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "rendering"


def _sample_report() -> Report:
    spec = RunSpec(product="groww", iso_week="2026-W16")
    themes = [
        Theme(
            theme_name="App *crash* & login",
            one_liner="Users report login-time crashes and freezes.",
            leadership_summary=(
                "Crashes on login block the primary activation path. "
                "This risks short-term retention and spikes support load."
            ),
            severity="high",
            confidence="high",
            quotes=[
                Quote(
                    review_id="play_store:groww:1",
                    text="App crashes\nright after login",
                ),
                Quote(
                    review_id="play_store:groww:2",
                    text="التطبيق يتعطل بعد تسجيل الدخول",
                ),
            ],
            action_ideas=[
                ActionIdea(
                    title="Patch login crash path",
                    rationale="Stabilize auth edge-cases on older devices.",
                    impact="Protects first-week retention.",
                )
            ],
            who_this_helps=[
                AudienceHelp(
                    audience="product",
                    why="Prioritizes a top-funnel reliability bug this sprint.",
                ),
                AudienceHelp(
                    audience="support",
                    why="Provides a clear known-issue response for login failures.",
                ),
            ],
            cluster_id=1,
            n_reviews=14,
        )
    ]
    return Report(
        spec=spec,
        themes=themes,
        window_label="Last 12 weeks",
        metrics={
            "reviews_analyzed": 373,
            "avg_rating": 4.2,
            "reviews_date_start": "2026-01-28",
            "reviews_date_end": "2026-04-22",
        },
    )


def test_anchor_id_is_deterministic() -> None:
    report = _sample_report()
    assert report.anchor_id == "pulse-groww-2026-W16"


def test_docops_snapshot() -> None:
    report = _sample_report()
    payload = docops_to_dict(render_docops(report))
    got = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    expected = (FIXTURES_DIR / "docops_groww.json").read_text(encoding="utf-8")
    assert got == expected


def test_email_html_snapshot() -> None:
    report = _sample_report()
    rendered = render_email(report)
    expected_html = (FIXTURES_DIR / "email_groww.html").read_text(encoding="utf-8")
    expected_text = (FIXTURES_DIR / "email_groww.txt").read_text(encoding="utf-8")
    assert rendered.subject == "[Pulse] Groww — 2026-W16"
    assert rendered.html_body == expected_html
    assert rendered.text_body == expected_text


def test_email_includes_deep_link_placeholder() -> None:
    rendered = render_email(_sample_report())
    assert rendered.deep_link_placeholder == "{{ deep_link }}"
    assert "{{ deep_link }}" in rendered.html_body
    assert "{{ deep_link }}" in rendered.text_body


def test_plain_text_always_present() -> None:
    rendered = render_email(_sample_report())
    assert rendered.html_body.strip()
    assert rendered.text_body.strip()


def test_theme_breakdown_trend_labels_present() -> None:
    spec = RunSpec(product="groww", iso_week="2026-W16")
    report = Report(
        spec=spec,
        themes=[
            Theme(
                theme_name="Theme A",
                one_liner="App crash and slow execution",
                leadership_summary="alpha",
                severity="high",
                confidence="high",
                quotes=[],
                action_ideas=[],
                who_this_helps=[],
                cluster_id=1,
                n_reviews=10,
            ),
            Theme(
                theme_name="Theme B",
                one_liner="Overall experience is smooth and positive",
                leadership_summary="beta",
                severity="low",
                confidence="medium",
                quotes=[],
                action_ideas=[],
                who_this_helps=[],
                cluster_id=2,
                n_reviews=8,
            ),
        ],
        window_label="Last 12 weeks",
    )
    text = render_email(report).text_body
    assert "Theme A — 10 mentions (Needs Attention)" in text
    assert "Theme B — 8 mentions (Positive Trend)" in text

