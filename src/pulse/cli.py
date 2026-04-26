"""pulse CLI — entry point for all pipeline commands."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

load_dotenv()  # pick up GROQ_API_KEY / LLM_PROVIDER from .env at import time

from pulse.config import load_products, load_pulse_config  # noqa: E402
from pulse.ingestion.app_store import AppStoreSource  # noqa: E402
from pulse.ingestion.base import Review  # noqa: E402
from pulse.ingestion.normalize import (  # noqa: E402
    content_hash,
    is_review_eligible,
    normalize,
)
from pulse.ingestion.play_store import PlayStoreSource  # noqa: E402
from pulse.observability.cleanup import cleanup_artifacts  # noqa: E402
from pulse.reasoning.cluster import cluster_reviews  # noqa: E402
from pulse.reasoning.embed import embed_reviews  # noqa: E402
from pulse.reasoning.theme import (  # noqa: E402
    ProviderError,
    ThemeGenerationStats,
    get_provider,
    name_themes,
    theme_to_dict,
)
from pulse.reasoning.validate import validate_themes  # noqa: E402
from pulse.rendering.report import build_report  # noqa: E402
from pulse.run import (  # noqa: E402
    Pipeline,
    build_run_spec,
    load_recent_run_records,
    load_run_record,
)
from pulse.safety.budget import BudgetExceeded, TokenBudget  # noqa: E402
from pulse.safety.scrub import Redaction, scrub  # noqa: E402
from pulse.storage.sqlite import ReviewStore  # noqa: E402
from pulse.web_portal import serve_portal  # noqa: E402

app = typer.Typer(
    name="pulse",
    help="Weekly Product Review Pulse — automated review insights via MCP.",
    no_args_is_help=True,
    add_completion=False,
)


# ── Shared options ────────────────────────────────────────────────────────────

_PRODUCT_OPTION = Annotated[
    str,
    typer.Option("--product", "-p", help="Product ID (e.g. groww)"),
]
_ISO_WEEK_OPTION = Annotated[
    str | None,
    typer.Option(
        "--iso-week", "-w",
        help="ISO week to process, e.g. 2026-W16. Defaults to current week.",
    ),
]
_DRY_RUN_FLAG = Annotated[
    bool,
    typer.Option("--dry-run", help="Resolve RunSpec and render artifacts without calling MCPs."),
]
_EMAIL_MODE_OPTION = Annotated[
    str | None,
    typer.Option(
        "--email-mode",
        help="Override email mode: draft | send. Defaults to pulse.yaml setting.",
    ),
]
_FORCE_FLAG = Annotated[
    bool,
    typer.Option(
        "--force",
        help="Generate a fresh run_id instead of the deterministic one (re-runs from scratch).",
    ),
]
_WEEKS_OPTION = Annotated[
    int,
    typer.Option(
        "--weeks",
        help="Rolling lookback window in weeks for ingestion (default: 12).",
    ),
]


# ── run ───────────────────────────────────────────────────────────────────────

@app.command()
def run(
    product: _PRODUCT_OPTION,
    iso_week: _ISO_WEEK_OPTION = None,
    dry_run: _DRY_RUN_FLAG = False,
    email_mode: _EMAIL_MODE_OPTION = None,
    force: _FORCE_FLAG = False,
) -> None:
    """Run the full weekly pulse pipeline for a product.

    In Phase 0 (bootstrap) this resolves and prints the RunSpec, then exits.
    Subsequent phases wire in ingestion, reasoning, rendering, and MCP delivery.
    """
    try:
        spec = build_run_spec(
            product=product,
            iso_week=iso_week,
            dry_run=dry_run,
            email_mode=email_mode,  # type: ignore[arg-type]
            force=force,
        )
    except (KeyError, ValueError) as exc:
        typer.echo(f"[pulse] Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(json.dumps(spec.to_dict(), indent=2))
    typer.echo()
    pipeline = Pipeline(spec)
    result = asyncio.run(pipeline.execute())
    typer.echo(json.dumps(result.to_dict(), indent=2))


# ── backfill ──────────────────────────────────────────────────────────────────

@app.command()
def backfill(
    product: _PRODUCT_OPTION,
    from_week: Annotated[str, typer.Option("--from", help="Start ISO week, e.g. 2026-W10")],
    to_week: Annotated[str, typer.Option("--to", help="End ISO week (inclusive), e.g. 2026-W12")],
    dry_run: _DRY_RUN_FLAG = False,
    email_mode: _EMAIL_MODE_OPTION = None,
) -> None:
    """Backfill every ISO week in [--from, --to] for a product.

    Expands the range to individual RunSpecs and prints them. Execution wired in Phase 6.
    """
    from pulse.run import _validate_iso_week

    try:
        _validate_iso_week(from_week)
        _validate_iso_week(to_week)
    except ValueError as exc:
        typer.echo(f"[pulse] Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    weeks = _expand_iso_week_range(from_week, to_week)
    if not weeks:
        typer.echo(f"[pulse] Error: --from {from_week} is after --to {to_week}.", err=True)
        raise typer.Exit(code=1)

    results = []
    for week in weeks:
        try:
            spec = build_run_spec(
                product=product,
                iso_week=week,
                dry_run=dry_run,
                email_mode=email_mode,  # type: ignore[arg-type]
            )
            result = asyncio.run(Pipeline(spec).execute())
            results.append(result.to_dict())
        except (KeyError, ValueError) as exc:
            typer.echo(f"[pulse] Error for {week}: {exc}", err=True)
            raise typer.Exit(code=1) from exc

    typer.echo()
    typer.echo(json.dumps(results, indent=2))
    msg = f"\n[pulse] Backfill: {len(results)} week(s) completed for product '{product}'."
    typer.echo(msg, err=True)


# ── status ────────────────────────────────────────────────────────────────────

@app.command()
def status(
    product: _PRODUCT_OPTION,
    last: Annotated[int, typer.Option("--last", "-n", help="Show last N run records.")] = 5,
) -> None:
    """Show the last N run records for a product."""
    records = load_recent_run_records(product, last=last)
    typer.echo(json.dumps({"product": product, "last": last, "records": records}, indent=2))


# ── ingest (Phase 1) ──────────────────────────────────────────────────────────


@app.command()
def ingest(
    product: _PRODUCT_OPTION,
    weeks: _WEEKS_OPTION = 12,
) -> None:
    """Ingest App Store + Play Store reviews and cache them in SQLite."""
    if weeks <= 0:
        typer.echo("[pulse] Error: --weeks must be > 0.", err=True)
        raise typer.Exit(code=1)

    try:
        registry = load_products()
        cfg = registry.get(product)
    except KeyError as exc:
        typer.echo(f"[pulse] Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    until = datetime.now(UTC)
    since = until - timedelta(weeks=weeks)

    db_path = _default_db_path()
    store = ReviewStore(db_path)
    store.migrate()

    app_store = AppStoreSource(
        app_id=cfg.app_store.app_id,
        country=cfg.app_store.country,
    )
    play_store = PlayStoreSource(
        package_name=cfg.play_store.package,
        lang=cfg.play_store.lang,
        country=cfg.play_store.country,
    )

    counts: dict[str, dict[str, object]] = {}
    warnings: list[str] = []
    for source_name, source in [("app_store", app_store), ("play_store", play_store)]:
        fetched = 0
        filtered_out = 0
        inserted_raw = 0
        inserted_normalized = 0
        scrubbed_redactions = 0
        filtered_reasons: dict[str, int] = {}
        latest_cursor: datetime | None = None

        try:
            for raw in source.fetch(product=product, since=since, until=until):
                fetched += 1
                review = normalize(raw)

                # Phase 2 wiring: scrub review text before downstream processing.
                scrubbed_body, body_redactions = scrub(review.body)
                scrubbed_title, title_redactions = scrub(review.title or "")
                scrubbed_redactions += len(body_redactions) + len(title_redactions)
                review = replace(
                    review,
                    body=scrubbed_body,
                    title=scrubbed_title or None,
                    content_hash=content_hash(scrubbed_title or None, scrubbed_body),
                )

                eligible, reason = is_review_eligible(raw, review)
                if not eligible:
                    filtered_out += 1
                    if reason:
                        filtered_reasons[reason] = filtered_reasons.get(reason, 0) + 1
                    continue

                if store.upsert_raw_review(raw):
                    inserted_raw += 1
                if store.upsert_review(review):
                    inserted_normalized += 1
                if latest_cursor is None or raw.posted_at > latest_cursor:
                    latest_cursor = raw.posted_at
        except Exception as exc:
            warnings.append(f"{source_name}_source_unavailable: {exc}")

        if latest_cursor is not None:
            store.set_fetch_cursor(product, source_name, latest_cursor.isoformat())

        counts[source_name] = {
            "fetched": fetched,
            "filtered_out": filtered_out,
            "filtered_reasons": filtered_reasons,
            "inserted_raw": inserted_raw,
            "inserted_reviews": inserted_normalized,
            "scrubbed_redactions": scrubbed_redactions,
            "total_cached": store.count_reviews(product, source_name),
        }

    output = {
        "product": product,
        "window_weeks": weeks,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "db_path": str(db_path),
        "counts": counts,
        "warnings": warnings,
        "total_cached_reviews": store.count_reviews(product),
    }
    typer.echo(json.dumps(output, indent=2))


# ── reason (Phase 3) ──────────────────────────────────────────────────────────


@app.command()
def reason(
    product: _PRODUCT_OPTION,
    iso_week: _ISO_WEEK_OPTION = None,
    weeks: _WEEKS_OPTION = 12,
    top_k: Annotated[int, typer.Option("--top-k", help="Number of themes to keep.")] = 3,
    provider_name: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Override LLM provider (groq | gemini | ollama). Defaults to LLM_PROVIDER env.",
        ),
    ] = None,
    random_state: Annotated[
        int, typer.Option("--seed", help="Deterministic seed for UMAP/HDBSCAN.")
    ] = 42,
) -> None:
    """Run Phase 3 over cached reviews: embed → cluster → theme → validate → report."""
    try:
        spec = build_run_spec(product=product, iso_week=iso_week, window_weeks=weeks)
    except (KeyError, ValueError) as exc:
        typer.echo(f"[pulse] Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    db_path = _default_db_path()
    store = ReviewStore(db_path)
    store.migrate()

    until = datetime.now(UTC)
    since = until - timedelta(weeks=spec.window_weeks)
    reviews = store.iter_reviews(product, since=since, until=until)

    pulse_cfg = load_pulse_config()
    min_reviews = pulse_cfg.run.min_reviews

    if len(reviews) < min_reviews:
        window_metrics = _review_window_metrics(reviews)
        report = build_report(
            spec,
            themes=[],
            window_label=f"Last {spec.window_weeks} weeks",
            total_reviews=len(reviews),
            reason="insufficient_reviews",
            metrics={
                "reviews_in_window": len(reviews),
                "min_reviews": min_reviews,
                **window_metrics,
            },
        )
        typer.echo(json.dumps(_report_to_dict(report), indent=2))
        return

    typer.echo(
        f"[pulse] Embedding {len(reviews)} reviews "
        f"({pulse_cfg.models.embedding})...",
        err=True,
    )
    embeddings = embed_reviews(
        reviews,
        store=store,
        model_name=pulse_cfg.models.embedding,
    )

    typer.echo("[pulse] Clustering...", err=True)
    clusters = cluster_reviews(
        reviews,
        embeddings,
        top_k=top_k,
        random_state=random_state,
    )

    if not clusters:
        window_metrics = _review_window_metrics(reviews)
        report = build_report(
            spec,
            themes=[],
            total_reviews=len(reviews),
            reason="no_clusters",
            metrics={"reviews_in_window": len(reviews), **window_metrics},
        )
        typer.echo(json.dumps(_report_to_dict(report), indent=2))
        return

    typer.echo(
        f"[pulse] Naming {len(clusters)} themes via provider "
        f"'{provider_name or os.environ.get('LLM_PROVIDER') or 'groq'}'...",
        err=True,
    )
    budget = TokenBudget(
        max_tokens_in=pulse_cfg.run.token_budget,
        max_tokens_out=8_000,
        max_requests=max(4, top_k * 3),
        provider="groq",
    )
    try:
        provider = get_provider(provider_name, budget=budget)
    except ProviderError as exc:
        typer.echo(f"[pulse] Provider error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    stats = ThemeGenerationStats()
    try:
        themes = name_themes(clusters, provider=provider, budget=budget, stats=stats)
    except BudgetExceeded as exc:
        typer.echo(f"[pulse] Budget exceeded: {exc}", err=True)
        window_metrics = _review_window_metrics(reviews)
        report = build_report(
            spec,
            themes=[],
            total_reviews=len(reviews),
            reason="budget_exceeded",
            metrics={
                "reviews_in_window": len(reviews),
                **window_metrics,
                **_stats_dict(stats),
            },
        )
        typer.echo(json.dumps(_report_to_dict(report), indent=2))
        raise typer.Exit(code=1) from exc

    reviews_by_id = {r.review_id: r for r in reviews}
    validated = validate_themes(themes, reviews_by_id)

    metrics: dict[str, object] = {
        "reviews_in_window": len(reviews),
        **_review_window_metrics(reviews),
        "clusters_found": len(clusters),
        "themes_before_validate": len(themes),
        "themes_after_validate": len(validated),
        **_stats_dict(stats),
    }
    report = build_report(
        spec,
        themes=validated,
        total_reviews=len(reviews),
        metrics=metrics,
    )
    typer.echo(json.dumps(_report_to_dict(report), indent=2))


def _stats_dict(stats: ThemeGenerationStats) -> dict[str, object]:
    return {
        "llm_attempts": stats.attempts,
        "llm_json_retries": stats.json_retries,
        "llm_dropped_invalid_json": stats.dropped_invalid_json,
        "llm_dropped_refusal": stats.dropped_refusal,
        "llm_rate_limited": stats.rate_limited,
        "llm_budget_exceeded": stats.budget_exceeded,
        "llm_errors": stats.errors,
    }


def _review_window_metrics(reviews: Sequence[Review]) -> dict[str, object]:
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


def _report_to_dict(report: object) -> dict[str, object]:
    from pulse.rendering.report import Report

    assert isinstance(report, Report)
    return {
        "run_spec": report.spec.to_dict(),
        "anchor_id": report.anchor_id,
        "window_label": report.window_label,
        "low_signal": report.low_signal,
        "reason": report.reason,
        "themes": [theme_to_dict(t) for t in report.themes],
        "metrics": report.metrics,
    }


# ── scrub (Phase 2) ───────────────────────────────────────────────────────────


@app.command("scrub")
def scrub_text(
    input_path: Annotated[
        Path,
        typer.Option(
            "--input",
            "-i",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
            help="Path to input text file for PII scrubbing.",
        ),
    ],
) -> None:
    """Scrub PII from a text file and print sanitized content plus redaction summary."""
    text = input_path.read_text(encoding="utf-8")
    scrubbed, redactions = scrub(text)
    payload = {
        "input_path": str(input_path),
        "redaction_count": len(redactions),
        "redaction_categories": _count_redaction_categories(redactions),
        "scrubbed_text": scrubbed,
    }
    typer.echo(json.dumps(payload, indent=2))


@app.command("portal")
def portal(
    host: Annotated[str, typer.Option("--host", help="Host to bind portal server.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to bind portal server.")] = 8780,
) -> None:
    """Serve the on-demand report webpage and trigger GitHub workflow_dispatch."""
    if port <= 0 or port > 65535:
        typer.echo("[pulse] Error: --port must be in 1..65535.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"[pulse] Portal running at http://{host}:{port}")
    serve_portal(host=host, port=port)


@app.command("diff")
def diff_runs(
    from_run: Annotated[str, typer.Option("--from", help="Base run_id.")],
    to_run: Annotated[str, typer.Option("--to", help="Target run_id.")],
) -> None:
    """Compare two run records and show theme-level deltas."""
    try:
        left = load_run_record(from_run)
        right = load_run_record(to_run)
    except FileNotFoundError as exc:
        typer.echo(f"[pulse] Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    def _themes(payload: dict[str, object]) -> list[dict[str, object]]:
        report = payload.get("report")
        if not isinstance(report, dict):
            return []
        raw = report.get("themes")
        if not isinstance(raw, list):
            return []
        return [t for t in raw if isinstance(t, dict)]

    a = _themes(left)
    b = _themes(right)
    a_names = {str(t.get("theme_name") or "") for t in a}
    b_names = {str(t.get("theme_name") or "") for t in b}
    out = {
        "from_run": from_run,
        "to_run": to_run,
        "added_themes": sorted(x for x in (b_names - a_names) if x),
        "removed_themes": sorted(x for x in (a_names - b_names) if x),
        "unchanged_themes": sorted(x for x in (a_names & b_names) if x),
    }
    typer.echo(json.dumps(out, indent=2))


@app.command("doctor")
def doctor() -> None:
    """Run lightweight repo hygiene checks (secrets + artifact health)."""
    root = Path.cwd()
    secret_patterns = [
        re.compile(r"gsk_[A-Za-z0-9]{20,}"),
        re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
        re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    ]
    checked_paths = [root / "src", root / "tests", root / "config"]
    findings: list[str] = []
    for base in checked_paths:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix in {".png", ".jpg", ".jpeg", ".gif", ".sqlite"}:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for patt in secret_patterns:
                if patt.search(text):
                    findings.append(str(path.relative_to(root)))
                    break
    data_dir = Path(os.environ.get("PULSE_DATA_DIR") or "data")
    artifacts_root = data_dir / "artifacts"
    artifact_dirs = (
        [p for p in artifacts_root.iterdir() if p.is_dir()] if artifacts_root.exists() else []
    )
    artifact_health = {
        "artifact_runs_found": len(artifact_dirs),
        "required_files": [
            "docops.json",
            "email.html",
            "email.txt",
            "clusters.json",
            "themes.json",
        ],
    }
    output = {
        "ok": len(findings) == 0,
        "secret_findings": findings,
        "artifact_health": artifact_health,
    }
    typer.echo(json.dumps(output, indent=2))
    if findings:
        raise typer.Exit(code=1)


@app.command("cleanup")
def cleanup(
    retention_days: Annotated[
        int, typer.Option("--retention-days", help="Delete artifacts older than this many days.")
    ] = 90,
) -> None:
    """Cleanup old data/artifacts directories by retention policy."""
    if retention_days <= 0:
        typer.echo("[pulse] Error: --retention-days must be > 0.", err=True)
        raise typer.Exit(code=1)
    data_dir = Path(os.environ.get("PULSE_DATA_DIR") or "data")
    result = cleanup_artifacts(data_dir / "artifacts", retention_days=retention_days)
    typer.echo(json.dumps(result, indent=2))


# ── ISO week helpers ──────────────────────────────────────────────────────────

def _expand_iso_week_range(from_week: str, to_week: str) -> list[str]:
    """Return every ISO week string in [from_week, to_week] inclusive."""
    from datetime import timedelta

    def _monday(iso_week: str) -> datetime:
        year, week_str = iso_week.split("-W")
        # ISO week Monday: use %G-W%V-%u
        return datetime.strptime(f"{year}-W{week_str}-1", "%G-W%V-%u")


    start = _monday(from_week)
    end = _monday(to_week)

    weeks = []
    current = start
    while current <= end:
        y, w, _ = current.isocalendar()
        weeks.append(f"{y}-W{w:02d}")
        current += timedelta(weeks=1)
    return weeks


def _default_db_path() -> Path:
    data_dir = os.environ.get("PULSE_DATA_DIR")
    root = Path(data_dir) if data_dir else Path("data")
    root.mkdir(parents=True, exist_ok=True)
    return root / "pulse.sqlite"


def _count_redaction_categories(redactions: list[Redaction]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for redaction in redactions:
        category = redaction.category
        counts[category] = counts.get(category, 0) + 1
    return counts


if __name__ == "__main__":
    app()
