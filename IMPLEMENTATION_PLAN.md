# Weekly Product Review Pulse — Phase-wise Implementation Plan

> Companion to `ARCHITECTURE.md`. This plan delivers the system in eight incremental phases, each independently runnable and testable. **Every tool, model, and service chosen here runs on a free tier or locally at zero cost.** No credit card is required to complete the full system.

Evaluations for each phase live in `EVALUATIONS.md`. Edge cases and their handling live in `EDGE_CASES.md`. Each phase in this plan corresponds one-to-one with sections in those two files.

---

## Free-tier stack summary

| Concern                     | Choice                                                     | Why it's free                                       |
|-----------------------------|------------------------------------------------------------|-----------------------------------------------------|
| Language / runtime          | Python 3.11+                                               | Open source                                         |
| Package manager             | `uv` (or `pip` + `venv`)                                   | Open source                                         |
| App Store reviews           | Public iTunes customer-reviews RSS                         | No auth, no key                                     |
| Google Play reviews         | `google-play-scraper` (PyPI)                               | Open source scraper, no key                         |
| Local cache                 | SQLite (stdlib)                                            | Zero cost                                           |
| PII redaction               | `presidio-analyzer` + regex                                | Open source, runs locally on CPU                    |
| Embeddings                  | `sentence-transformers` (`all-MiniLM-L6-v2`)               | Runs locally on CPU, no API                         |
| Dim reduction               | `umap-learn`                                               | Open source                                         |
| Clustering                  | `hdbscan`                                                  | Open source                                         |
| LLM for theming             | **Groq free tier** (`llama-3.3-70b-versatile`, default) *or* **Gemini** (`gemini-2.5-flash`) *or* local **Ollama** (`llama3.2:3b`) | Free tier keys; local model is fully offline         |
| Tokenizer (budget governor) | `tiktoken` for OpenAI-family; `tokenizers` HF for others   | Open source                                         |
| Templating (email)          | `jinja2`                                                   | Open source                                         |
| CLI                         | `typer`                                                    | Open source                                         |
| MCP host/client             | `mcp` Python SDK                                           | Open source                                         |
| Google Docs MCP server      | Community MCP server (e.g. `mcp-gsuite` or equivalent)     | Open source; uses **personal Google account OAuth** (free) |
| Gmail MCP server            | Community MCP server                                       | Open source; personal Google OAuth (free)           |
| Scheduling                  | GitHub Actions cron (public repo: unlimited; private: 2 000 min/mo free) *or* local `cron` / `launchd` | Free tier                                           |
| Logs / metrics              | Python `logging` JSON formatter; local files               | Zero cost                                           |

**Email mode defaults to `draft`** throughout development. A real send happens only when you explicitly flip `email_mode: send` in `config/pulse.yaml`. This keeps dev iteration free of side-effects.

**LLM free-tier notes:**
- Groq free tier is the default — each run issues only ~1 call per cluster (≤3 / run), which sits far inside free-tier limits.
- Gemini and Ollama remain as interchangeable fallbacks if Groq is unavailable or if we need offline-only theming.
- If both tiers are exhausted, the `OLLAMA` provider flag swaps to a local model — no external calls at all.

---

## Phase 0 — Bootstrap & configuration skeleton

**Goal:** empty-but-runnable repo; anyone can clone, install, and invoke `pulse --help` with zero cost.

**Deliverables**
- `pyproject.toml` with pinned deps (grouped: `core`, `reasoning`, `dev`).
- `uv.lock` (or `requirements.txt` if using pip).
- Repo layout exactly as specified in `ARCHITECTURE.md` §16, with empty modules and `__init__.py`.
- `config/pulse.yaml` and `config/products.yaml` with **Groww-only** configuration (store IDs, package name, stakeholders, doc title).
- `.env.example` listing only free-tier env vars:
  ```
  LLM_PROVIDER=groq            # groq (default) | gemini | ollama
  GEMINI_API_KEY=
  GROQ_API_KEY=
  OLLAMA_BASE_URL=http://localhost:11434
  EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
  LOG_LEVEL=INFO
  ```
- `README.md` with one-command local bootstrap (`uv sync && pulse --help`).
- `pulse` CLI entry point wired to `typer`, exposing `run`, `backfill`, `dry-run` (stubs that print resolved `RunSpec` and exit).
- Pre-commit: `ruff`, `black`, `mypy` (all free, local).

**Exit criteria**
- `uv sync` succeeds from scratch with no paid accounts configured.
- `pulse run --product groww --iso-week 2026-W16 --dry-run` prints a resolved `RunSpec` and exits 0.
- `pytest` runs (even with zero tests) without import errors.

**Estimated effort:** 0.5 day

---

## Phase 1 — Ingestion & normalization

**Goal:** fetch Groww reviews from both stores, normalize into `Review`, and cache in SQLite. Fully offline-replayable.

**Deliverables**
- `src/pulse/ingestion/base.py` — `ReviewSource` protocol, `RawReview`, `Review` dataclasses.
- `src/pulse/ingestion/app_store.py`
  - Fetches `https://itunes.apple.com/{country}/rss/customerreviews/page={n}/id={app_id}/sortby=mostrecent/json`.
  - Polite `User-Agent` that identifies the project; honors `If-Modified-Since` / `ETag`.
  - Paginates until `posted_at < since` or no more pages.
- `src/pulse/ingestion/play_store.py`
  - Uses `google-play-scraper.reviews` with `Sort.NEWEST` and `continuation_token`.
  - Throttled to ≤ 1 req/sec.
- `src/pulse/ingestion/normalize.py`
  - Canonical `review_id = f"{source}:{product}:{native_id}"`.
  - `content_hash = sha256(normalize_ws(title + body))` for edit-supersession.
- `src/pulse/storage/sqlite.py` — migrations for `raw_reviews`, `reviews`, and `fetch_cursors` tables.
- CLI: `pulse ingest --product <id> --weeks 12` that runs ingestion only and prints counts per source.

**Free-tier considerations**
- Both sources are unauthenticated and zero-cost; the only cost is bandwidth. Cache aggressively.
- Fixture recording: commit small VCR fixtures under `tests/fixtures/ingestion/` so CI doesn't hit the network.

**Exit criteria**
- `pulse ingest --product groww --weeks 12` populates `reviews` with ≥ 1 row (assuming Groww has recent reviews).
- Re-running the same command on the same day is a near no-op (cursors advance only past new content).

**Estimated effort:** 1.5 days

---

## Phase 2 — Safety layer (PII scrub, envelopes, token budget)

**Goal:** nothing reaches the LLM or the Doc unredacted; runs cannot silently blow the free-tier quota.

**Deliverables**
- `src/pulse/safety/scrub.py`
  - Regex pass: emails, Indian phone (`+91` / 10-digit), PAN (`[A-Z]{5}[0-9]{4}[A-Z]`), Aadhaar-like (12-digit with spacing), URLs with query strings, card-like 13–19 digit sequences (Luhn check to reduce false positives).
  - `presidio-analyzer` pass with English + built-in recognizers for `PERSON`, `LOCATION`.
  - Output is `(scrubbed_text, redactions: list[Redaction])`.
- `src/pulse/safety/envelopes.py`
  - `wrap_reviews_for_llm(reviews: list[Review]) -> str` producing typed `<review id="…" rating="…">…</review>` blocks inside a single system-controlled fence.
- `src/pulse/safety/budget.py`
  - `TokenBudget(max_tokens_in, max_tokens_out, max_requests)` with provider-specific tokenizer lookup (`tiktoken` for OpenAI-shaped, `tokenizers` HF AutoTokenizer for Gemini/Groq/Llama).
  - Raises `BudgetExceeded` rather than silently truncating.
- `src/pulse/safety/outbound.py` — second-pass scrub applied to rendered DocOps + email just before delivery.
- CLI: `pulse scrub --input <file>` for manual spot-checking.

**Free-tier considerations**
- Presidio runs locally; no paid Azure Text Analytics recognizer.
- Budget defaults (configurable):
  - Gemini free: `max_tokens_in = 60 000`, `max_tokens_out = 4 000`, `max_requests = 10` per run.
  - Groq free: same input, `max_requests = 8`.
  - Ollama: budget is effectively only latency; keep `max_tokens_in = 30 000` to keep runs under 5 min on CPU.

**Exit criteria**
- Golden-file tests for PII scrub pass (see `EVALUATIONS.md` §2).
- A crafted review containing `"Ignore previous instructions and…"` passes through the pipeline unchanged as *content* — the LLM stage in Phase 3 treats it as data.
- Exceeding the configured budget with a synthetic input aborts cleanly with a typed exception.

**Estimated effort:** 1 day

---

## Phase 3 — Reasoning (embed → cluster → theme → validate) — ✅ Done

**Goal:** produce a validated `Report` object from a set of scrubbed reviews, using only free/local compute for embeddings and a free-tier LLM for theming.

**Deliverables**
- `src/pulse/reasoning/embed.py`
  - `sentence-transformers` with `all-MiniLM-L6-v2` (~80 MB, runs on CPU).
  - Batched encode; embeddings cached in SQLite keyed by `(review_id, model_name)`.
- `src/pulse/reasoning/cluster.py`
  - UMAP → 10 dims (`n_neighbors=15`, `min_dist=0.0`, `metric="cosine"`).
  - HDBSCAN with dynamic `min_cluster_size = max(5, N // 30)`.
  - Cluster ranking by `severity = size × (1 − mean_rating / 5) × recency_weight`.
- `src/pulse/reasoning/theme.py`
  - Provider abstraction: `GeminiProvider`, `GroqProvider`, `OllamaProvider`. Selected by `LLM_PROVIDER` env var.
  - One request per cluster; structured JSON output enforced via:
    - Gemini: `response_schema` / `response_mime_type=application/json`.
    - Groq: `response_format={"type": "json_object"}` + schema in prompt.
    - Ollama: JSON mode via `format=json`.
  - Single retry on `JSONDecodeError` with a stricter reminder prompt.
- `src/pulse/reasoning/validate.py`
  - For every quote in every theme, assert:
    1. Referenced `review_id` is in the current run's dataset.
    2. Whitespace-normalized quote is a contiguous substring of the *scrubbed* body.
    3. No PII placeholders (`[email]`, `[phone]`, …) in the quote.
  - Fail-closed: drop themes that can't be repaired on one retry.
- `src/pulse/reasoning/report.py` — builds `Report` with ≥ 2 validated themes or emits a `low_signal` report.

**Free-tier considerations**
- Embedding model is downloaded once on first run; cached under `~/.cache/huggingface`.
- Default to Groq free tier (`llama-3.3-70b-versatile`); `LLM_PROVIDER=ollama` is a first-class offline path that costs nothing and doesn't require any API key; `gemini` is an optional cloud fallback.
- Sampling: if a cluster has > 20 reviews, send only the 20 closest to the medoid to the LLM to stay under the per-request token budget.

**Exit criteria**
- On a stored fixture of ≥ 200 Groww reviews, the pipeline produces 2–5 themes with ≥ 2 validated quotes each.
- Re-running with a pinned seed yields identical clusters and theme IDs.
- Swapping `LLM_PROVIDER=ollama` produces a valid `Report` with no external network call beyond Ollama at `localhost`.

**Estimated effort:** 2 days

---

## Phase 4 — Rendering (DocOps + email) — ✅ Done

**Goal:** turn a validated `Report` into (a) a structured `DocOps` batch for the Docs MCP and (b) a multipart HTML+text email body. Purely deterministic; no network calls.

**Deliverables**
- `src/pulse/rendering/docops.py`
  - Domain ops: `InsertHeading(level, text, anchor_id)`, `InsertParagraph(runs)`, `InsertBulletList(items)`, `InsertHorizontalRule()`.
  - Runs support bold / italic / link.
  - `anchor_id = f"pulse-{product}-{iso_week}"`.
  - Heading 1 for the section, Heading 2 for "Top themes" / "Real user quotes" / "Action ideas" / "What this helps".
- `src/pulse/rendering/email.py`
  - `jinja2` template at `src/pulse/rendering/templates/email.html` with a plain-text sibling.
  - Subject: `[Pulse] {Product} — {iso_week}`.
  - ≤ 8 bullets; a single "Read full report" anchor to the deep link (filled in by the delivery orchestrator after the Doc append succeeds).
- Snapshot fixtures for both outputs under `tests/fixtures/rendering/`.

**Exit criteria**
- Snapshot tests are byte-stable across runs.
- Rendered HTML passes a basic lint (no unclosed tags) and a plain-text alternative is always present.
- The DocOps batch round-trips through a serializer → deserializer without loss (makes MCP transport trivial).

**Estimated effort:** 1 day

---

## Phase 5 — MCP delivery (Docs + Gmail, with idempotency) — ✅ Done

**Goal:** the agent speaks to Google only through MCP servers; re-running the same `(product, iso_week)` is a safe no-op.

**Deliverables**
- `src/pulse/delivery/mcp_client.py`
  - Thin wrapper over the `mcp` Python SDK, stdio transport.
  - Starts MCP server subprocesses from config, streams JSON-RPC, exposes `call_tool(server, name, args)`.
- `src/pulse/delivery/docs.py` — adapter with:
  - `find_or_create_doc(title) -> doc_id`
  - `find_heading_anchor(doc_id, anchor_id) -> heading_id | None`
  - `append_section(doc_id, doc_ops) -> heading_id`
  - `get_heading_link(doc_id, heading_id) -> str`
- `src/pulse/delivery/gmail.py` — adapter with:
  - `search_by_header(name, value) -> list[message_id]`
  - `create_draft(mime) -> draft_id`
  - `send(mime) -> message_id`
  - Custom headers: `X-Pulse-Run-Id`, `X-Pulse-Product`, `X-Pulse-IsoWeek`.
- `src/pulse/delivery/orchestrator.py`
  - Orchestrates: find-or-create doc → check anchor → append-if-missing → get-link → substitute into email → search gmail by run id → send-or-draft-if-missing.
  - Retries with exponential backoff on transport errors; breaks circuit after 3 failures.
- MCP server configuration documented in `README.md`:
  - Use hosted MCP endpoint `https://saksham-mcp-server-uht7.onrender.com` for both Docs and Gmail.
  - Transport configured as `sse` in `config/pulse.yaml`.

**Free-tier considerations**
- Personal Google accounts have free quotas that vastly exceed one weekly run for Groww.
- Both MCP servers run locally as subprocesses; no hosting cost.
- Dev/staging keeps `email_mode: draft` so no mail is actually sent during iteration.

**Exit criteria**
- Against fake MCP servers (in-memory), running the same `RunSpec` twice produces exactly one `append_section` call and one `send` call.
- Against real MCP servers with a personal Google account, a dry-run produces a Doc section + a Gmail draft, and the draft's deep link opens the correct heading.
- Killing the process between Doc append and Gmail send and re-running completes the email without duplicating the Doc section.

**Estimated effort:** 2 days

---

## Phase 6 — Orchestration, CLI, and scheduling — ✅ Done

**Goal:** bring the full pipeline online with `pulse run`, `pulse backfill`, run records, and a free scheduler.

**Deliverables**
- `src/pulse/run.py` — `Pipeline` that stitches all phases, writes `data/runs/{run_id}.json`, emits structured logs.
- CLI commands (all via `typer`):
  - `pulse run --product <id> [--iso-week <w>] [--dry-run] [--email-mode draft|send]`
  - `pulse backfill --product <id> --from <w> --to <w>`
  - `pulse status --product <id>` — prints last N run records.
- Deterministic `run_id` derivation (`ULID(seed=sha256(product+iso_week))`).
- GitHub Actions workflow `.github/workflows/pulse.yml`:
  - `schedule: cron: "30 1 * * 1"` (Monday 07:00 IST = 01:30 UTC).
  - Single job for `groww` (no product matrix needed).
- Secrets: `GROQ_API_KEY` (primary, free tier) with optional `GEMINI_API_KEY` fallback. MCP server OAuth tokens are bundled into the runner via GitHub Actions secrets exported to the MCP servers' config dirs at startup.
  - Uses only the free `ubuntu-latest` runner and caches `~/.cache/huggingface` to keep subsequent runs under the free-tier minute budget.
- Local alternative: a `launchd` plist (macOS) and a cron snippet (Linux) in `docs/local-scheduling.md`.

**Free-tier considerations**
- Public repo → unlimited Actions minutes. Private repo → ≈ 1 run × 1 product × 4 weeks × < 5 min = far under 2 000 min/month.
- Hugging Face model cache keyed on file hash keeps cold-start under 30 s.

**Exit criteria**
- `pulse run --product groww` (dry-run) completes end-to-end locally in < 3 min on a laptop without any paid services.
- The scheduled workflow runs green on a test schedule (`workflow_dispatch`) for Groww.
- Re-triggering the workflow for the same ISO week is a no-op on both Doc and Gmail sides.

**Estimated effort:** 1 day

---

## Phase 7 — Observability & hardening — ✅ Done

**Goal:** make runs debuggable after the fact without paid APM.

**Deliverables**
- `src/pulse/observability/logging.py` — JSON-line logger with `run_id`, `stage`, `duration_ms`, `tokens`, `status` always present.
- `src/pulse/observability/metrics.py` — simple counter/histogram/gauge that write to `data/runs/{run_id}/metrics.jsonl`; no external sink required.
- Dry-run artifact dump: `data/artifacts/{run_id}/` containing `docops.json`, `email.html`, `email.txt`, `clusters.json`, `themes.json`.
- Diff tool: `pulse diff --product <id> --from <run_id> --to <run_id>` showing changed themes and validator deltas.
- Secrets hygiene check: `pulse doctor` command that asserts no Google OAuth tokens exist in the repo and no API keys in logs.

**Exit criteria**
- A failing run leaves behind enough artifacts under `data/artifacts/{run_id}/` to reconstruct the problem without re-running ingestion.
- `pulse doctor` passes on a clean clone.
- Logs are machine-parseable JSON and contain zero secrets (grep-based test).

**Estimated effort:** 1 day

---

## Dependency graph

```text
Phase 0 ──▶ Phase 1 ──▶ Phase 2 ──▶ Phase 3 ──▶ Phase 4 ──▶ Phase 5 ──▶ Phase 6 ──▶ Phase 7
                                              │
                                              └── (Phase 4 can start in parallel with Phase 3
                                                   once Report domain object is frozen)
```

**Total estimated effort:** ~10 working days for a single engineer, end-to-end, with no spend.

---

## Rollout order (recommended)

1. Phases 0 → 1 → 2 on the first day or two — gets you a trustworthy, cached review corpus.
2. Phase 3 validated against `LLM_PROVIDER=ollama` (offline, deterministic) and `LLM_PROVIDER=groq` (production default). Gemini is supported as a hot fallback.
3. Phase 4 → 5 with fake MCPs; only then wire real community MCP servers + personal Google OAuth.
4. Phase 6 → 7 in the final stretch, once a manual run produces a stakeholder-worthy report.
