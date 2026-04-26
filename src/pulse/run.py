"""RunSpec, deterministic IDs, and Phase-6 pipeline orchestration."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pulse.config import ProductRegistry, PulseConfig, load_products, load_pulse_config
from pulse.observability.logging import get_run_logger
from pulse.observability.metrics import MetricsCollector

if TYPE_CHECKING:
    from pulse.ingestion.base import Review
    from pulse.rendering.docops import DocOps
    from pulse.rendering.email import RenderedEmail
    from pulse.rendering.report import Report

# ── Types ─────────────────────────────────────────────────────────────────────

ProductId = str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _derive_run_id(product: ProductId, iso_week: str) -> str:
    """Return a deterministic, stable 40-char hex ID for a (product, iso_week) pair.

    Using SHA-256 so that retries of the same run converge on the same identifier.
    Pass ``force=True`` via the factory to get a fresh time-based ID instead.
    """
    digest = hashlib.sha256(f"{product}:{iso_week}".encode()).hexdigest()
    return digest[:40]


def _fresh_run_id(product: ProductId, iso_week: str) -> str:
    """Return a non-deterministic ID (for --force re-runs)."""
    now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    digest = hashlib.sha256(f"{product}:{iso_week}:{now}".encode()).hexdigest()[:16]
    return f"force-{digest}"


def _current_iso_week() -> str:
    """Return the current ISO week string, e.g. '2026-W16'."""
    today = datetime.now(UTC)
    year, week, _ = today.isocalendar()
    return f"{year}-W{week:02d}"


def _validate_iso_week(iso_week: str) -> None:
    """Raise ValueError for malformed ISO week strings."""
    import re

    pattern = r"^\d{4}-W(?:0[1-9]|[1-4]\d|5[0-3])$"
    if not re.match(pattern, iso_week):
        raise ValueError(
            f"Invalid ISO week '{iso_week}'. Expected format: YYYY-Www (e.g. 2026-W16)."
        )


# ── RunSpec ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RunSpec:
    product: ProductId
    iso_week: str
    window_weeks: int = 12
    run_id: str = field(default="")
    dry_run: bool = False
    email_mode: Literal["draft", "send"] = "draft"

    def __post_init__(self) -> None:
        _validate_iso_week(self.iso_week)
        # Populate run_id after frozen init via object.__setattr__
        if not self.run_id:
            object.__setattr__(self, "run_id", _derive_run_id(self.product, self.iso_week))

    def to_dict(self) -> dict[str, object]:
        return {
            "product": self.product,
            "iso_week": self.iso_week,
            "window_weeks": self.window_weeks,
            "run_id": self.run_id,
            "dry_run": self.dry_run,
            "email_mode": self.email_mode,
        }


def build_run_spec(
    product: ProductId,
    iso_week: str | None,
    *,
    window_weeks: int | None = None,
    dry_run: bool = False,
    email_mode: Literal["draft", "send"] | None = None,
    force: bool = False,
    registry: ProductRegistry | None = None,
    pulse_cfg: PulseConfig | None = None,
) -> RunSpec:
    """Validate inputs against the product registry and build a RunSpec."""
    if registry is None:
        registry = load_products()
    if pulse_cfg is None:
        pulse_cfg = load_pulse_config()

    # Validate product
    registry.get(product)  # raises KeyError for unknown product

    resolved_week = iso_week or _current_iso_week()
    resolved_window = window_weeks or pulse_cfg.run.window_weeks
    resolved_mode = email_mode or pulse_cfg.run.email_mode

    if force:
        run_id = _fresh_run_id(product, resolved_week)
    else:
        run_id = _derive_run_id(product, resolved_week)

    return RunSpec(
        product=product,
        iso_week=resolved_week,
        window_weeks=resolved_window,
        run_id=run_id,
        dry_run=dry_run,
        email_mode=resolved_mode,
    )


@dataclass
class PipelineResult:
    run_id: str
    product: str
    iso_week: str
    status: str
    dry_run: bool
    report: dict[str, object]
    delivery: dict[str, object]
    timings_ms: dict[str, int]
    warnings: list[str]
    started_at: str
    finished_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _data_dir() -> Path:
    configured = os.environ.get("PULSE_DATA_DIR")
    root = Path(configured) if configured else Path("data")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _db_path() -> Path:
    return _data_dir() / "pulse.sqlite"


def _runs_dir() -> Path:
    path = _data_dir() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _artifacts_dir(run_id: str) -> Path:
    path = _data_dir() / "artifacts" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _parse_doc_id(value: str | None) -> str | None:
    if not value:
        return None
    if "/document/d/" in value:
        try:
            return value.split("/document/d/")[1].split("/")[0]
        except IndexError:
            return None
    return value


def _review_window_metrics(reviews: list[Review]) -> dict[str, object]:
    if not reviews:
        return {
            "reviews_analyzed": 0,
            "avg_rating": 0.0,
            "reviews_date_start": "",
            "reviews_date_end": "",
        }
    avg_rating = round(sum(r.rating for r in reviews) / len(reviews), 2)
    start = min(r.posted_at for r in reviews).date().isoformat()
    end = max(r.posted_at for r in reviews).date().isoformat()
    return {
        "reviews_analyzed": len(reviews),
        "avg_rating": avg_rating,
        "reviews_date_start": start,
        "reviews_date_end": end,
    }


class Pipeline:
    """Phase 6 pipeline: ingest -> reason -> render -> deliver -> run record."""

    def __init__(
        self,
        spec: RunSpec,
        *,
        registry: ProductRegistry | None = None,
        pulse_cfg: PulseConfig | None = None,
    ) -> None:
        self.spec = spec
        self.registry = registry or load_products()
        self.pulse_cfg = pulse_cfg or load_pulse_config()
        self.product_cfg = self.registry.get(spec.product)
        from pulse.storage.sqlite import ReviewStore

        self.store = ReviewStore(_db_path())
        self.store.migrate()

    async def execute(self) -> PipelineResult:
        started = datetime.now(UTC)
        timings: dict[str, int] = {}
        warnings: list[str] = []
        ingest_counts: dict[str, object]
        logger = get_run_logger(self.spec.run_id, _data_dir())
        metrics = MetricsCollector(self.spec.run_id, _data_dir())

        if self.spec.dry_run:
            ingest_counts = {"status": "skipped_dry_run"}
            logger.info(
                "ingest stage skipped",
                extra={"run_id": self.spec.run_id, "stage": "ingest", "status": "skipped"},
            )
            metrics.counter("stage_invocations", stage="ingest", status="skipped")
        else:
            t0 = datetime.now(UTC)
            ingest_counts = self._ingest_window(warnings=warnings)
            timings["ingest"] = int((datetime.now(UTC) - t0).total_seconds() * 1000)
            logger.info(
                "ingest stage complete",
                extra={
                    "run_id": self.spec.run_id,
                    "stage": "ingest",
                    "status": "ok",
                    "duration_ms": timings["ingest"],
                },
            )
            metrics.counter("stage_invocations", stage="ingest", status="ok")
            metrics.histogram("stage_duration_ms", timings["ingest"], stage="ingest")

        t1 = datetime.now(UTC)
        report = self._reason_report()
        timings["reason"] = int((datetime.now(UTC) - t1).total_seconds() * 1000)
        logger.info(
            "reason stage complete",
            extra={
                "run_id": self.spec.run_id,
                "stage": "reason",
                "status": "ok",
                "duration_ms": timings["reason"],
            },
        )
        metrics.counter("stage_invocations", stage="reason", status="ok")
        metrics.histogram("stage_duration_ms", timings["reason"], stage="reason")
        metrics.gauge("themes_after_validate", float(len(report.themes)), stage="reason")

        t2 = datetime.now(UTC)
        from pulse.reasoning.theme import theme_to_dict
        from pulse.rendering.docops import render_docops
        from pulse.rendering.email import render_email

        docops = render_docops(report)
        email = render_email(report)
        timings["render"] = int((datetime.now(UTC) - t2).total_seconds() * 1000)
        logger.info(
            "render stage complete",
            extra={
                "run_id": self.spec.run_id,
                "stage": "render",
                "status": "ok",
                "duration_ms": timings["render"],
            },
        )
        metrics.counter("stage_invocations", stage="render", status="ok")
        metrics.histogram("stage_duration_ms", timings["render"], stage="render")

        delivery: dict[str, object] = {"mode": self.spec.email_mode}
        status = "ok"
        if not self.spec.dry_run and self.pulse_cfg.mcp is not None:
            t3 = datetime.now(UTC)
            delivery = await self._deliver(docops=docops, email=email)
            timings["deliver"] = int((datetime.now(UTC) - t3).total_seconds() * 1000)
            logger.info(
                "deliver stage complete",
                extra={
                    "run_id": self.spec.run_id,
                    "stage": "deliver",
                    "status": "ok",
                    "duration_ms": timings["deliver"],
                },
            )
            metrics.counter("stage_invocations", stage="deliver", status="ok")
            metrics.histogram("stage_duration_ms", timings["deliver"], stage="deliver")
        else:
            delivery["status"] = "skipped_dry_run"
            logger.info(
                "deliver stage skipped",
                extra={"run_id": self.spec.run_id, "stage": "deliver", "status": "skipped"},
            )
            metrics.counter("stage_invocations", stage="deliver", status="skipped")

        finished = datetime.now(UTC)
        report_payload = {
            "run_spec": self.spec.to_dict(),
            "anchor_id": report.anchor_id,
            "window_label": report.window_label,
            "low_signal": report.low_signal,
            "reason": report.reason,
            "themes_count": len(report.themes),
            "metrics": report.metrics,
            "docops_count": len(docops),
            "email_subject": email.subject,
            "ingest_counts": ingest_counts,
            "themes": [theme_to_dict(t) for t in report.themes],
        }
        result = PipelineResult(
            run_id=self.spec.run_id,
            product=self.spec.product,
            iso_week=self.spec.iso_week,
            status=status,
            dry_run=self.spec.dry_run,
            report=report_payload,
            delivery=delivery,
            timings_ms=timings,
            warnings=warnings,
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
        )
        if self.spec.dry_run:
            artifact_warning = self._write_artifacts(
                report=report_payload,
                docops=docops,
                email=email,
            )
            if artifact_warning:
                warnings.append(artifact_warning)
        self._write_run_record(result)
        return result

    def _ingest_window(self, *, warnings: list[str]) -> dict[str, object]:
        from pulse.ingestion.app_store import AppStoreSource
        from pulse.ingestion.normalize import content_hash, is_review_eligible, normalize
        from pulse.ingestion.play_store import PlayStoreSource
        from pulse.safety.scrub import scrub

        until = datetime.now(UTC)
        since = until - timedelta(weeks=self.spec.window_weeks)
        app_store = AppStoreSource(
            app_id=self.product_cfg.app_store.app_id,
            country=self.product_cfg.app_store.country,
        )
        play_store = PlayStoreSource(
            package_name=self.product_cfg.play_store.package,
            lang=self.product_cfg.play_store.lang,
            country=self.product_cfg.play_store.country,
        )

        out: dict[str, object] = {}
        for source_name, source in (("app_store", app_store), ("play_store", play_store)):
            fetched = 0
            inserted = 0
            filtered = 0
            latest_cursor: datetime | None = None
            try:
                for raw in source.fetch(product=self.spec.product, since=since, until=until):
                    fetched += 1
                    review = normalize(raw)
                    body, _ = scrub(review.body)
                    title, _ = scrub(review.title or "")
                    review = replace(
                        review,
                        body=body,
                        title=title or None,
                        content_hash=content_hash(title or None, body),
                    )
                    eligible, _ = is_review_eligible(raw, review)
                    if not eligible:
                        filtered += 1
                        continue
                    self.store.upsert_raw_review(raw)
                    if self.store.upsert_review(review):
                        inserted += 1
                    if latest_cursor is None or raw.posted_at > latest_cursor:
                        latest_cursor = raw.posted_at
            except Exception as exc:  # pragma: no cover - network dependent
                warnings.append(f"{source_name}_source_unavailable: {exc}")
            if latest_cursor is not None:
                self.store.set_fetch_cursor(
                    self.spec.product, source_name, latest_cursor.isoformat()
                )
            out[source_name] = {"fetched": fetched, "inserted": inserted, "filtered": filtered}
        return out

    def _reason_report(self) -> Report:
        from pulse.reasoning.cluster import cluster_reviews
        from pulse.reasoning.embed import embed_reviews
        from pulse.reasoning.theme import (
            ProviderError,
            ThemeGenerationStats,
            get_provider,
            name_themes,
        )
        from pulse.reasoning.validate import validate_themes
        from pulse.rendering.report import build_report
        from pulse.safety.budget import BudgetExceeded, TokenBudget

        until = datetime.now(UTC)
        since = until - timedelta(weeks=self.spec.window_weeks)
        reviews = self.store.iter_reviews(self.spec.product, since=since, until=until)
        if len(reviews) < self.pulse_cfg.run.min_reviews:
            return build_report(
                self.spec,
                themes=[],
                total_reviews=len(reviews),
                reason="insufficient_reviews",
                metrics={"reviews_in_window": len(reviews), **_review_window_metrics(reviews)},
            )
        embeddings = embed_reviews(
            reviews,
            store=self.store,
            model_name=self.pulse_cfg.models.embedding,
        )
        clusters = cluster_reviews(
            reviews,
            embeddings,
            top_k=self.pulse_cfg.run.top_k_themes,
            random_state=42,
        )
        if not clusters:
            return build_report(
                self.spec,
                themes=[],
                total_reviews=len(reviews),
                reason="no_clusters",
                metrics={"reviews_in_window": len(reviews), **_review_window_metrics(reviews)},
            )
        budget = TokenBudget(
            max_tokens_in=self.pulse_cfg.run.token_budget,
            max_tokens_out=8_000,
            max_requests=max(4, self.pulse_cfg.run.top_k_themes * 3),
            provider="groq",
        )
        stats = ThemeGenerationStats()
        try:
            provider = get_provider(budget=budget)
            themes = name_themes(clusters, provider=provider, budget=budget, stats=stats)
        except (ProviderError, BudgetExceeded):  # pragma: no cover - provider/network dependent
            themes = []
        reviews_by_id = {r.review_id: r for r in reviews}
        validated = validate_themes(themes, reviews_by_id)
        metrics = {
            "reviews_in_window": len(reviews),
            **_review_window_metrics(reviews),
            "clusters_found": len(clusters),
            "themes_before_validate": len(themes),
            "themes_after_validate": len(validated),
            "llm_attempts": stats.attempts,
        }
        return build_report(
            self.spec,
            themes=validated,
            total_reviews=len(reviews),
            metrics=metrics,
        )

    async def _deliver(self, *, docops: DocOps, email: RenderedEmail) -> dict[str, object]:
        from pulse.delivery.docs import DocsAdapter
        from pulse.delivery.gmail import GmailAdapter
        from pulse.delivery.mcp_client import MCPClient
        from pulse.delivery.orchestrator import DeliveryOrchestrator

        assert self.pulse_cfg.mcp is not None
        docs_client = MCPClient(
            command=self.pulse_cfg.mcp.docs.command,
            transport=self.pulse_cfg.mcp.docs.transport,
        )
        gmail_client = MCPClient(
            command=self.pulse_cfg.mcp.gmail.command,
            transport=self.pulse_cfg.mcp.gmail.transport,
        )
        async with docs_client, gmail_client:
            docs = DocsAdapter(docs_client)
            gmail = GmailAdapter(gmail_client)
            orchestrator = DeliveryOrchestrator(docs=docs, gmail=gmail)
            recipients = [s.email for s in self.product_cfg.stakeholders] or [
                s.email for s in self.pulse_cfg.stakeholders_default
            ]
            # Phase 6 policy: create/find a dedicated weekly doc each run.
            weekly_doc_title = f"{self.product_cfg.doc_title} — {self.spec.iso_week}"
            result: dict[str, object] = await orchestrator.deliver(
                run_id=self.spec.run_id,
                product=self.spec.product,
                iso_week=self.spec.iso_week,
                doc_title=weekly_doc_title,
                doc_id_override=None,
                anchor_id=f"pulse-{self.spec.product}-{self.spec.iso_week}",
                doc_ops=docops,
                email=email,
                email_mode=self.spec.email_mode,
                recipients=recipients,
            )
            result["recipients"] = recipients
            return result

    def _write_run_record(self, result: PipelineResult) -> None:
        path = _runs_dir() / f"{result.run_id}.json"
        path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")

    def _write_artifacts(
        self,
        *,
        report: dict[str, object],
        docops: DocOps,
        email: RenderedEmail,
    ) -> str:
        from pulse.rendering.docops import docops_to_dict

        artifacts_dir = _artifacts_dir(self.spec.run_id)
        warning = ""
        if any(artifacts_dir.iterdir()) and not self.spec.run_id.startswith("force-"):
            warning = "artifacts_overwrite_existing_run_id"
        (artifacts_dir / "report.json").write_text(
            json.dumps(report, indent=2) + "\n",
            encoding="utf-8",
        )
        (artifacts_dir / "docops.json").write_text(
            json.dumps(docops_to_dict(docops), indent=2) + "\n",
            encoding="utf-8",
        )
        (artifacts_dir / "email.html").write_text(email.html_body, encoding="utf-8")
        (artifacts_dir / "email.txt").write_text(email.text_body, encoding="utf-8")
        themes_payload = report.get("themes")
        (artifacts_dir / "themes.json").write_text(
            json.dumps(themes_payload if isinstance(themes_payload, list) else [], indent=2) + "\n",
            encoding="utf-8",
        )
        (artifacts_dir / "clusters.json").write_text("[]\n", encoding="utf-8")
        return warning


def load_recent_run_records(product: str, last: int = 5) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(_runs_dir().glob("*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if str(payload.get("product", "")) != product:
            continue
        records.append(payload)
        if len(records) >= last:
            break
    return records


def load_run_record(run_id: str) -> dict[str, object]:
    path = _runs_dir() / f"{run_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"run record not found: {run_id}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"invalid run record payload for run_id={run_id}")
    return {str(k): v for k, v in payload.items()}


__all__ = [
    "Pipeline",
    "PipelineResult",
    "RunSpec",
    "build_run_spec",
    "load_run_record",
    "load_recent_run_records",
    "_derive_run_id",
]
