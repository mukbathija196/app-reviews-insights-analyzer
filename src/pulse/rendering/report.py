"""Report domain object assembled from validated themes."""

from __future__ import annotations

from dataclasses import dataclass, field

from pulse.reasoning.theme import Theme
from pulse.run import RunSpec

MIN_THEMES_FOR_SIGNAL = 2


@dataclass
class Report:
    """Assembled run artifact handed off to rendering/delivery."""

    spec: RunSpec
    themes: list[Theme]
    low_signal: bool = False
    window_label: str = ""
    reason: str = ""  # populated when low_signal=True
    metrics: dict[str, object] = field(default_factory=dict)

    @property
    def anchor_id(self) -> str:
        return f"pulse-{self.spec.product}-{self.spec.iso_week}"


def build_report(
    spec: RunSpec,
    themes: list[Theme],
    *,
    window_label: str = "",
    total_reviews: int = 0,
    reason: str = "",
    metrics: dict[str, object] | None = None,
) -> Report:
    """Build a Report and mark it ``low_signal`` when below the floor.

    A report with fewer than ``MIN_THEMES_FOR_SIGNAL`` validated themes is
    returned as-is but with ``low_signal=True``. Rendering/delivery layers can
    then choose to emit a "not enough signal this week" section instead of
    fabricating content (EC-3.1, EC-3.4).
    """
    low_signal = len(themes) < MIN_THEMES_FOR_SIGNAL
    inferred_reason = reason
    if low_signal and not inferred_reason:
        inferred_reason = (
            "insufficient_themes"
            if total_reviews > 0
            else "insufficient_reviews"
        )

    effective_label = window_label or f"Last {spec.window_weeks} weeks"

    return Report(
        spec=spec,
        themes=list(themes),
        low_signal=low_signal,
        window_label=effective_label,
        reason=inferred_reason,
        metrics=dict(metrics or {}),
    )


__all__ = ["MIN_THEMES_FOR_SIGNAL", "Report", "build_report"]
