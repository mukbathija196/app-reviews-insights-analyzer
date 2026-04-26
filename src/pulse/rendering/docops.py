"""Structured DocOps batch for Google Docs MCP."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pulse.rendering.report import Report

# ── Primitive ops ─────────────────────────────────────────────────────────────

@dataclass
class TextRun:
    text: str
    bold: bool = False
    italic: bool = False
    link: str | None = None


@dataclass
class InsertHeading:
    op: Literal["heading"] = "heading"
    level: int = 1
    text: str = ""
    anchor_id: str = ""


@dataclass
class InsertParagraph:
    op: Literal["paragraph"] = "paragraph"
    runs: list[TextRun] = field(default_factory=list)


@dataclass
class InsertBulletList:
    op: Literal["bullet_list"] = "bullet_list"
    items: list[str] = field(default_factory=list)


@dataclass
class InsertHorizontalRule:
    op: Literal["hr"] = "hr"


DocOp = InsertHeading | InsertParagraph | InsertBulletList | InsertHorizontalRule
DocOps = list[DocOp]


# ── Renderer ──────────────────────────────────────────────────────────────────

def _clean_text(text: str) -> str:
    """Collapse inner newlines/whitespace for render-safe single-line runs."""
    return " ".join(text.split())


def _low_signal_message(reason: str) -> str:
    if reason == "insufficient_reviews":
        return "Not enough recent reviews this week to publish reliable themes."
    if reason == "insufficient_themes":
        return "Too few validated themes this week; report intentionally marked low-signal."
    if reason:
        return f"Low-signal report: {reason}."
    return "Low-signal report: insufficient reliable signal this week."


def render_docops(report: Report) -> DocOps:
    """Produce a deterministic DocOps batch from a Report."""
    date_start = str(report.metrics.get("reviews_date_start", "") or "").strip()
    date_end = str(report.metrics.get("reviews_date_end", "") or "").strip()
    date_range_text = ""
    if date_start and date_end:
        date_range_text = f"  Date range: {date_start} to {date_end}"
    elif date_start:
        date_range_text = f"  Date range: {date_start}"
    elif date_end:
        date_range_text = f"  Date range: {date_end}"

    ops: DocOps = [
        InsertHeading(
            level=1,
            text=f"Weekly Review Pulse — {report.spec.product.title()} ({report.spec.iso_week})",
            anchor_id=report.anchor_id,
        ),
        InsertParagraph(
            runs=[
                TextRun(text=f"Window: {report.window_label}{date_range_text}  "),
                TextRun(text=f"Run ID: {report.spec.run_id}", italic=True),
            ]
        ),
        InsertHorizontalRule(),
    ]

    if report.low_signal:
        ops.extend(
            [
                InsertHeading(level=2, text="Top themes"),
                InsertParagraph(runs=[TextRun(text=_low_signal_message(report.reason))]),
            ]
        )
        return ops

    ops.append(InsertHeading(level=2, text="Top themes"))
    for idx, theme in enumerate(report.themes, start=1):
        ops.append(
            InsertParagraph(
                runs=[
                    TextRun(text=f"{idx}. {theme.theme_name}", bold=True),
                    TextRun(
                        text=(
                            f"  [{theme.severity}/{theme.confidence}] "
                            f"{_clean_text(theme.one_liner)}"
                        )
                    ),
                ]
            )
        )
        if theme.leadership_summary:
            ops.append(InsertParagraph(runs=[TextRun(text=_clean_text(theme.leadership_summary))]))
        ops.append(InsertHeading(level=3, text="Real user quotes"))
        quote_items = [f"\"{_clean_text(q.text)}\"" for q in theme.quotes[:3]]
        if quote_items:
            ops.append(InsertBulletList(items=quote_items))
        else:
            ops.append(
                InsertParagraph(runs=[TextRun(text="No publishable quotes for this theme.")])
            )

        ops.append(InsertHeading(level=3, text="Action ideas"))
        action_items = [
            f"{_clean_text(a.title)} — {_clean_text(a.rationale)}"
            + (f" Impact: {_clean_text(a.impact)}" if a.impact else "")
            for a in theme.action_ideas[:3]
        ]
        if action_items:
            ops.append(InsertBulletList(items=action_items))
        else:
            ops.append(InsertParagraph(runs=[TextRun(text="No action ideas for this theme.")]))

        ops.append(InsertHeading(level=3, text="Who this helps"))
        help_items = [f"{w.audience.title()}: {_clean_text(w.why)}" for w in theme.who_this_helps]
        if help_items:
            ops.append(InsertBulletList(items=help_items))
        else:
            ops.append(
                InsertParagraph(runs=[TextRun(text="No audience guidance for this theme.")])
            )

    return ops


def docops_to_dict(docops: DocOps) -> list[dict[str, Any]]:
    """Serialize DocOps into JSON-safe dicts for MCP transport/artifacts."""
    out: list[dict[str, Any]] = []
    for op in docops:
        if isinstance(op, InsertHeading):
            out.append(
                {
                    "op": op.op,
                    "level": op.level,
                    "text": op.text,
                    "anchor_id": op.anchor_id,
                }
            )
            continue
        if isinstance(op, InsertParagraph):
            out.append(
                {
                    "op": op.op,
                    "runs": [
                        {"text": r.text, "bold": r.bold, "italic": r.italic, "link": r.link}
                        for r in op.runs
                    ],
                }
            )
            continue
        if isinstance(op, InsertBulletList):
            out.append({"op": op.op, "items": list(op.items)})
            continue
        out.append({"op": op.op})
    return out


def docops_from_dict(payload: list[dict[str, Any]]) -> DocOps:
    """Deserialize JSON-safe dicts back into DocOps."""
    out: DocOps = []
    for item in payload:
        op = str(item.get("op") or "")
        if op == "heading":
            out.append(
                InsertHeading(
                    level=int(item.get("level") or 1),
                    text=str(item.get("text") or ""),
                    anchor_id=str(item.get("anchor_id") or ""),
                )
            )
            continue
        if op == "paragraph":
            runs_raw = item.get("runs") or []
            runs = [
                TextRun(
                    text=str(r.get("text") or ""),
                    bold=bool(r.get("bold", False)),
                    italic=bool(r.get("italic", False)),
                    link=str(r["link"]) if r.get("link") is not None else None,
                )
                for r in runs_raw
                if isinstance(r, dict)
            ]
            out.append(InsertParagraph(runs=runs))
            continue
        if op == "bullet_list":
            items = [str(x) for x in (item.get("items") or [])]
            out.append(InsertBulletList(items=items))
            continue
        if op == "hr":
            out.append(InsertHorizontalRule())
            continue
        raise ValueError(f"Unknown doc op: {op}")
    return out


__all__ = [
    "TextRun",
    "InsertHeading",
    "InsertParagraph",
    "InsertBulletList",
    "InsertHorizontalRule",
    "DocOp",
    "DocOps",
    "docops_from_dict",
    "docops_to_dict",
    "render_docops",
]
