"""Run record persistence (JSON files under data/runs/). Implemented in Phase 6."""

from __future__ import annotations

from pathlib import Path


class RunStore:
    """Reads and writes run records to data/runs/{run_id}.json.

    Implemented fully in Phase 6.
    """

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir

    def save(self, run_id: str, record: dict[str, object]) -> None:
        """Persist a run record. Implemented in Phase 6."""
        raise NotImplementedError("Implemented in Phase 6.")

    def load(self, run_id: str) -> dict[str, object]:
        """Load a run record by run_id. Implemented in Phase 6."""
        raise NotImplementedError("Implemented in Phase 6.")

    def list_for_product(self, product: str, last: int = 10) -> list[dict[str, object]]:
        """Return the last N run records for a product. Implemented in Phase 6."""
        raise NotImplementedError("Implemented in Phase 6.")


__all__ = ["RunStore"]
