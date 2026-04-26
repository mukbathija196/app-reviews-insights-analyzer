"""HTML + plain-text email rendering via Jinja2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pulse.rendering.report import Report


@dataclass
class RenderedEmail:
    subject: str
    html_body: str
    text_body: str
    deep_link_placeholder: str = "{{ deep_link }}"


def _clean_text(text: str) -> str:
    return " ".join(text.split())


def _pretty_date(iso_date: str) -> str:
    try:
        y, m, d = iso_date.split("-")
        return date(int(y), int(m), int(d)).strftime("%d %b %Y")
    except (ValueError, TypeError):
        return iso_date


def _as_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _classify_trend(severity: str, one_liner: str) -> str:
    text = one_liner.lower()
    negative_markers = (
        "crash",
        "loss",
        "issue",
        "slow",
        "lag",
        "error",
        "charge",
        "fail",
        "frustrat",
        "bad",
        "poor",
    )
    positive_markers = ("love", "great", "good", "smooth", "fast", "excellent", "positive")
    if severity == "high" or any(token in text for token in negative_markers):
        return "Needs Attention"
    if severity == "low" and any(token in text for token in positive_markers):
        return "Positive Trend"
    return "Neutral Trend"


def _build_context(report: Report) -> dict[str, Any]:
    themed_sections = [
        {
            "theme_name": theme.theme_name,
            "one_liner": _clean_text(theme.one_liner),
            "leadership_summary": _clean_text(theme.leadership_summary),
            "severity": theme.severity,
            "confidence": theme.confidence,
            "quotes": [
                {"text": _clean_text(q.text), "review_id": q.review_id} for q in theme.quotes[:3]
            ],
            "actions": [
                {
                    "title": _clean_text(a.title),
                    "rationale": _clean_text(a.rationale),
                    "impact": _clean_text(a.impact),
                }
                for a in theme.action_ideas[:3]
            ],
            "helps": [
                {"audience": w.audience.title(), "why": _clean_text(w.why)}
                for w in theme.who_this_helps
            ],
        }
        for theme in report.themes[:8]
    ]
    metrics = report.metrics
    reviews_analyzed = _as_int(
        metrics.get("reviews_analyzed", metrics.get("reviews_in_window", 0)) or 0
    )
    avg_rating = _as_float(metrics.get("avg_rating", 0.0) or 0.0)
    date_start = str(metrics.get("reviews_date_start", "") or "")
    date_end = str(metrics.get("reviews_date_end", "") or "")
    date_range = ""
    if date_start and date_end:
        date_range = f"{_pretty_date(date_start)} - {_pretty_date(date_end)}"
    elif date_start:
        date_range = _pretty_date(date_start)
    elif date_end:
        date_range = _pretty_date(date_end)

    return {
        "subject": f"[Pulse] {report.spec.product.title()} — {report.spec.iso_week}",
        "product_name": report.spec.product.title(),
        "product": report.spec.product,
        "iso_week": report.spec.iso_week,
        "window_label": report.window_label,
        "run_id": report.spec.run_id,
        "deep_link": "{{ deep_link }}",
        "themes": themed_sections,
        "reviews_analyzed": reviews_analyzed,
        "avg_rating": avg_rating,
        "date_range": date_range,
        "top_theme_name": themed_sections[0]["theme_name"] if themed_sections else "No clear theme",
        "theme_breakdown": [
            {
                "name": section["theme_name"],
                "mentions": theme.n_reviews,
                "trend": _classify_trend(
                    str(section.get("severity", "")),
                    str(section.get("one_liner", "")),
                ),
            }
            for section, theme in zip(themed_sections, report.themes, strict=False)
        ][:3],
        "low_signal": report.low_signal,
        "low_signal_reason": report.reason,
    }


def render_email(report: Report) -> RenderedEmail:
    """Render deterministic multipart email output from a Report."""
    templates_dir = Path(__file__).resolve().parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    context = _build_context(report)
    html_body = env.get_template("email.html").render(**context).strip() + "\n"
    text_body = env.get_template("email.txt").render(**context).strip() + "\n"

    return RenderedEmail(
        subject=str(context["subject"]),
        html_body=html_body,
        text_body=text_body,
    )


__all__ = ["RenderedEmail", "render_email"]
