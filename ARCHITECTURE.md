# Weekly Product Review Pulse — Architecture

> Companion to `problemStatement.md`. This document specifies the end‑to‑end system: components, data flow, contracts, storage, MCP integration, idempotency, safety, and deployment.

---

## 1. Architectural goals

The design optimizes for five non-negotiables that fall directly out of the problem statement:

1. **Deterministic, idempotent weekly runs** — a `(iso_week)` key (with product fixed to Groww) produces exactly one Doc section and at most one email, regardless of retries.
2. **MCP-mediated delivery** — the agent never holds Google OAuth secrets; all Docs/Gmail writes flow through dedicated MCP servers.
3. **Grounded reasoning** — every quote published must exist verbatim in a real review; every theme must be backed by a cluster of ≥ N reviews.
4. **Auditability** — every run emits a run record with input fingerprint, output identifiers (doc heading id, gmail message id), and cost/latency metrics.
5. **Modular separation of concerns** — ingestion, reasoning, rendering, and delivery are independently testable and replaceable.

---

## 2. High-level component diagram

```text
                ┌──────────────────────────────────────────────────────────┐
                │                  Scheduler / CLI                         │
                │   (cron @ Mon 07:00 IST  |  `pulse run --product ...`)   │
                └───────────────────────────┬──────────────────────────────┘
                                            │ RunSpec(product, iso_week, window)
                                            ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                              Pulse Agent (Host)                              │
│                                                                              │
│   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌─────────────┐   │
│   │  Ingestion   │──▶│  Normalize   │──▶│  PII Scrub   │──▶│   Cache     │   │
│   │  (AppStore + │   │   & Dedup    │   │              │   │ (SQLite/FS) │   │
│   │   Play)      │   └──────────────┘   └──────────────┘   └──────┬──────┘   │
│   └──────────────┘                                                │          │
│                                                                   ▼          │
│                     ┌──────────────────────────────────────────────────┐     │
│                     │   Reasoning                                      │     │
│                     │   embeddings → UMAP → HDBSCAN → LLM naming       │     │
│                     │   + quote-grounding validator                    │     │
│                     └────────────────────────┬─────────────────────────┘     │
│                                              ▼                               │
│                     ┌──────────────────────────────────────────────────┐     │
│                     │   Rendering                                      │     │
│                     │   DocOps (structured) + HTML/text email          │     │
│                     └────────────────────────┬─────────────────────────┘     │
│                                              ▼                               │
│                     ┌──────────────────────────────────────────────────┐     │
│                     │   Delivery Orchestrator                          │     │
│                     │   - idempotency keys / anchors                   │     │
│                     │   - retry & circuit breaker                      │     │
│                     └───────────┬──────────────────────────┬───────────┘     │
│                                 │ MCP JSON-RPC             │ MCP JSON-RPC    │
└─────────────────────────────────┼──────────────────────────┼─────────────────┘
                                  ▼                          ▼
                      ┌────────────────────┐     ┌────────────────────┐
                      │ Google Docs MCP    │     │  Gmail MCP         │
                      │  (own OAuth)       │     │  (own OAuth)       │
                      └─────────┬──────────┘     └─────────┬──────────┘
                                ▼                          ▼
                          Google Docs API              Gmail API
                                │                          │
                                ▼                          ▼
                   "Weekly Review Pulse — <Product>"   Stakeholder inbox
                   (append-only running doc)           (teaser + deep link)
```

---

## 3. Runtime model

### 3.1 Trigger surface

Two entry points share the same core pipeline:

| Surface    | Use case                          | Contract                                                     |
|------------|-----------------------------------|--------------------------------------------------------------|
| Scheduler  | Weekly cadence (Mon 07:00 IST)    | Enqueues one `RunSpec` for Groww                             |
| CLI        | Backfill / re-run any ISO week    | `pulse run --product groww --iso-week 2026-W16 [--dry-run]`  |

Both resolve to a single immutable `RunSpec`:

```python
@dataclass(frozen=True)
class RunSpec:
    product: ProductId            # "groww" (project scope for now)
    iso_week: str                 # e.g. "2026-W16"
    window_weeks: int             # 8..12, default 12
    run_id: str                   # ULID, stable per (product, iso_week) unless --force
    dry_run: bool                 # if true: render but do not call MCPs
    email_mode: Literal["draft", "send"]
```

The `run_id` is derived deterministically as `ULID(seed = sha256(product + iso_week))` unless `--force` is passed, so retries naturally converge. In this project, `product` is fixed to `groww`.

### 3.2 Pipeline stages

Each stage has a pure function signature and a durable cache keyed by `run_id + stage`. A stage is re-executed only if its inputs changed or the cache entry is missing.

```
ingest → normalize → scrub → embed → cluster → theme → validate → render → deliver
```

Failures after `render` do not re-execute earlier stages on retry — this is what makes a re-run cheap and idempotent.

---

## 4. Ingestion layer

### 4.1 Sources

| Source           | Mechanism                                                    | Rate posture                        |
|------------------|--------------------------------------------------------------|-------------------------------------|
| Apple App Store  | Public customer-reviews RSS (`/rss/customerreviews/...`)     | Polite polling, ETag/If-Modified-Since |
| Google Play      | `google-play-scraper` equivalent (HTML endpoint)             | Throttled, exponential backoff, UA rotation off (identify honestly) |

Each source is a `ReviewSource` that implements:

```python
class ReviewSource(Protocol):
    source_id: Literal["app_store", "play_store"]
    def fetch(self, product: ProductId, since: datetime, until: datetime) -> Iterable[RawReview]: ...
```

### 4.2 Product configuration

The project is currently **Groww-only** and is configured in `config/products.yaml`:

```yaml
groww:
  display_name: "Groww"
  doc_title: "Weekly Review Pulse — Groww"
  app_store:
    country: "in"
    app_id: "1404871703"
  play_store:
    package: "com.nextbillion.groww"
    lang: "en"
    country: "in"
  stakeholders:
    - name: "PM Lead"
      email: "pm-lead@example.com"
```

Adding more products is intentionally out of scope for this build. If needed later, it can still be introduced as a config-only extension.

### 4.3 Normalization

Heterogeneous source payloads are normalized to a single `Review`:

```python
@dataclass(frozen=True)
class Review:
    review_id: str        # stable: "{source}:{product}:{native_id}"
    product: ProductId
    source: Literal["app_store", "play_store"]
    rating: int           # 1..5
    title: str | None
    body: str             # scrubbed before leaving the ingestion layer
    lang: str             # BCP-47
    country: str          # ISO-3166-1 alpha-2
    posted_at: datetime   # UTC
    app_version: str | None
    fetched_at: datetime  # UTC
```

Dedup key: `review_id`. Late-arriving edits are handled by `(review_id, content_hash)` — a newer hash supersedes.

### 4.4 Caching

Raw and normalized reviews are persisted in a local SQLite file (`data/pulse.sqlite`) under tables `raw_reviews` and `reviews`. This lets the reasoning stage be re-run without re-hitting stores, and lets backfills of overlapping windows benefit from earlier fetches.

---

## 5. Safety layer (before reasoning)

Reviews are untrusted user content and cross two trust boundaries: into the LLM, and into a stakeholder-visible Doc. The safety layer runs **once**, immediately after normalization.

1. **PII scrub** — regex + `presidio` (or equivalent) pass redacts emails, phone numbers, account numbers, Aadhaar/PAN-like patterns, URLs with query tokens. Redacted fragments are replaced with typed placeholders (`[email]`, `[phone]`).
2. **Prompt-injection neutering** — reviews are wrapped in a fenced, typed envelope before reaching the LLM, and the system prompt declares them as data, not instructions:
   ```text
   <review id="..." rating="3">
   ...scrubbed body...
   </review>
   ```
3. **Length clamp** — individual reviews truncated to a budget (e.g. 2 000 chars) before embedding, to bound cost.
4. **Cost governor** — a per-run `TokenBudget` caps total input + output tokens. The governor short-circuits the pipeline with a clear error rather than silently degrading.

Scrubbed text is what is persisted; the raw body stays in `raw_reviews` only and is never sent to the LLM or Docs.

---

## 6. Reasoning layer

### 6.1 Embedding

All scrubbed review bodies (plus title) are embedded with a single provider call per run (batched). Model is configurable — e.g. `text-embedding-3-small` — and pinned per run in the run record.

### 6.2 Clustering

- **Dim reduction**: UMAP to ~10 dims (`n_neighbors=15`, `min_dist=0.0`, `metric="cosine"`).
- **Cluster**: HDBSCAN (`min_cluster_size = max(5, N_reviews // 30)`, `min_samples=3`).
- **Noise**: HDBSCAN label `-1` is excluded from theming.
- **Ranking**: clusters ranked by `severity = size × (1 - mean_rating/5) × recency_weight`, where `recency_weight` favors the most recent 3 weeks of the window.

The top K clusters (default K=3, capped at 5) become candidate themes.

### 6.3 Theme naming + action ideas (LLM)

One LLM call per top cluster, with a strict JSON schema output:

```json
{
  "theme_name": "string (<=60 chars, sentence case)",
  "one_liner": "string (<=160 chars)",
  "representative_quote_ids": ["review_id", "..."],
  "action_ideas": [
    { "title": "string", "rationale": "string" }
  ],
  "who_this_helps": ["product" | "support" | "leadership"]
}
```

The LLM only receives:
- The scrubbed, envelope-wrapped review texts in that cluster (capped to ~20 most central by distance to medoid).
- A system prompt that forbids inventing quotes and requires returning `review_id`s.

### 6.4 Quote-grounding validator

Before rendering, every quote the report will publish must pass:

1. Referenced `review_id` exists in this run's dataset.
2. The quoted string is a contiguous substring (after whitespace normalization) of that review's scrubbed body.
3. After PII scrub, the quote contains no placeholders that would render as `[email]` etc.

Any theme that fails validation is either repaired via a single retry ("re-select from these exact reviews") or dropped. A report is only publishable if at least 2 validated themes remain; otherwise the run emits a `low_signal` report clearly marked as such, rather than fabricating.

---

## 7. Rendering layer

Rendering produces two artifacts from a single `Report` domain object:

### 7.1 `DocOps` — structured instructions for Google Docs MCP

Rather than emitting raw markdown and hoping, the renderer emits a list of Docs batchUpdate-style operations that the MCP tool consumes. This keeps formatting (headings, bullets, links, bold) explicit and reviewable.

```python
DocOps = list[DocOp]  # e.g. InsertHeading, InsertParagraph, InsertBulletList, InsertNamedAnchor
```

Critically, the renderer emits a **named heading anchor** for the week's section:

```
anchor_id = f"pulse-{product}-{iso_week}"   # e.g. "pulse-groww-2026-W16"
heading_text = f"{iso_week} · {window_label}"
```

The anchor is how idempotency and deep-linking both work.

### 7.2 Email body — HTML + text multipart

- Subject: `[Pulse] {Product} — {iso_week}`
- Body: ≤ 8 bullets (top themes + one-liners), a "Read full report" link pointing at `https://docs.google.com/document/d/{docId}#heading={anchorId}`, and a short footer with run metadata.
- A text/plain alternative is always included.

---

## 8. Delivery layer (MCP)

The agent is an MCP **host/client**. It does not call Google REST APIs directly. Two servers are required:

### 8.1 Google Docs MCP server

Tools used by the agent:

| Tool                          | Purpose                                             |
|-------------------------------|-----------------------------------------------------|
| `docs.find_or_create`         | Get (or create) the Groww running doc by title |
| `docs.find_heading_anchor`    | Check if `anchor_id` already exists → idempotency    |
| `docs.append_section`         | Apply the `DocOps` batch as a new trailing section   |
| `docs.get_heading_link`       | Return the deep link for the just-created anchor     |

Contract expectations: `append_section` is transactional from the agent's perspective — it returns the heading's object id or fails; partial appends must be rolled back by the server or detectable by the agent.

### 8.2 Gmail MCP server

Tools used by the agent:

| Tool                | Purpose                                                   |
|---------------------|-----------------------------------------------------------|
| `gmail.search`      | Look up prior messages by the run's `X-Pulse-Run-Id`       |
| `gmail.create_draft`| Staging / dev path; returns `draftId`                     |
| `gmail.send`        | Production path; returns `messageId`                      |

Every message carries custom headers that make audits trivial:

```
X-Pulse-Run-Id: 01JABCXYZ...
X-Pulse-Product: groww
X-Pulse-IsoWeek: 2026-W16
```

### 8.3 Why MCP and not direct APIs

- Google OAuth tokens live in the MCP servers' configs, never in the agent process or repo.
- The tool surface is narrow and auditable — the agent can only do what the MCP exposes.
- Swapping Workspace for another backend (e.g. Notion + Slack) is a config change, not a rewrite.

---

## 9. Idempotency model

Two independent idempotency guards, one per delivery channel:

### 9.1 Doc section (anchor-based)

Before calling `docs.append_section`, the agent calls `docs.find_heading_anchor(anchor_id)`:

- **Hit** → skip append; reuse returned heading id/link.
- **Miss** → append; store the returned heading id in the run record.

Because the anchor is derived deterministically from `(product, iso_week)`, concurrent or retried runs converge on the same section.

### 9.2 Email (run-id based)

Before sending, `gmail.search("X-Pulse-Run-Id:{run_id} in:anywhere")`:

- **Hit** → skip; reuse `messageId`.
- **Miss** → create draft or send per `email_mode`.

If a run is interrupted between Doc append and email send, the next run still does only what's missing.

### 9.3 Run record

```jsonc
// data/runs/{run_id}.json
{
  "run_id": "01JABCXYZ...",
  "product": "groww",
  "iso_week": "2026-W16",
  "window_weeks": 12,
  "started_at": "2026-04-20T01:30:00Z",
  "finished_at": "2026-04-20T01:33:41Z",
  "input_fingerprint": "sha256:...",   // hash of review_ids used
  "models": { "embedding": "text-embedding-3-small", "llm": "..." },
  "cost": { "tokens_in": 48123, "tokens_out": 3102, "usd": 0.14 },
  "outputs": {
    "doc_id": "1AbC...",
    "heading_id": "h.7xyz",
    "anchor_id": "pulse-groww-2026-W16",
    "deep_link": "https://docs.google.com/...#heading=h.7xyz",
    "email": { "mode": "send", "message_id": "<...@mail.gmail.com>" }
  },
  "themes": [ { "name": "...", "n_reviews": 42, "validated_quotes": 3 } ],
  "status": "ok"
}
```

The run record is the audit trail that answers "what was sent when, for which week?"

---

## 10. Storage

Small, embedded, replaceable:

| Store                    | Content                                          | Why                                         |
|--------------------------|--------------------------------------------------|---------------------------------------------|
| `data/pulse.sqlite`      | `raw_reviews`, `reviews`, `embeddings`           | Cheap caching; survives re-runs             |
| `data/runs/*.json`       | Run records                                      | Human-readable audit trail                  |
| `data/artifacts/{run_id}/` | Rendered `DocOps`, email HTML, cluster dump   | Debuggability + post-mortem                 |
| `config/products.yaml`   | Groww product config                             | Single-product project configuration        |

No secrets are stored here. Google OAuth is owned by the MCP servers. LLM / embedding API keys live in environment variables consumed by the agent.

---

## 11. Observability

- **Structured logs** — JSON lines with `run_id`, `stage`, `duration_ms`, `tokens`, `status`.
- **Metrics** (stdout or OTLP) — counters per stage, histogram of latency, gauge of reviews ingested per source, counter of validator rejections.
- **Tracing** — one span per stage, plus child spans per MCP tool call. Errors on MCP calls are tagged with the tool name.
- **Dry-run report** — `--dry-run` writes the `DocOps` and email HTML to `data/artifacts/{run_id}/` and emits a diff vs. the last real run for the same product, with zero MCP calls.

---

## 12. Failure modes and responses

| Failure                                              | Response                                                                 |
|------------------------------------------------------|--------------------------------------------------------------------------|
| Source fetch fails (one of two)                      | Proceed with available source; mark report accordingly; log a warning    |
| Both sources fail                                    | Abort with non-zero exit; no Doc/email write                             |
| Too few reviews after scrub (< min_reviews)          | Emit a `low_signal` section; still append to Doc; email clearly labeled  |
| LLM returns invalid JSON                             | One retry with stricter schema reminder; else drop that theme            |
| Quote validator rejects all themes                   | Abort render; run marked `failed_validation`; no delivery                |
| Docs MCP append fails mid-way                        | Do not attempt email; next run's idempotency resumes from missing anchor |
| Gmail MCP fails after successful Doc append         | Next run detects anchor present but no `messageId` and sends only email  |
| Token budget exceeded                                | Abort before publish; run marked `budget_exceeded`                       |

---

## 13. Security and privacy

- **Least privilege** — Docs MCP is scoped to `documents` (no Drive-wide read); Gmail MCP is scoped to `send`/`compose` only. No `drive.readonly` or `gmail.readonly` beyond what the idempotency checks require.
- **Secrets** — no Google credentials in the repo or agent env. LLM keys are loaded from env and never logged.
- **PII** — scrubbed at ingest; raw bodies never leave the local cache and are purged after 90 days.
- **Prompt injection** — review text is delivered inside typed envelopes; the system prompt declares reviews as data.
- **Output safety** — the rendered Doc/email text is passed through an outbound scrub before delivery to catch anything the first pass missed.

---

## 14. Configuration surface

```yaml
# config/pulse.yaml
run:
  window_weeks: 12
  min_reviews: 40
  top_k_themes: 3
  token_budget: 250000
  email_mode: draft        # "draft" in dev/staging, "send" in prod

models:
  embedding: text-embedding-3-small
  llm: <configurable>

mcp:
  docs:
    command: "uvx google-docs-mcp"
    transport: stdio
  gmail:
    command: "uvx gmail-mcp"
    transport: stdio

stakeholders_default:
  - name: "Product"
    email: "product@example.com"
```

Current project configuration (Groww doc title, stakeholders, and store IDs) lives in `config/products.yaml`.

---

## 15. Deployment

- **Packaging** — single Python package; `pulse` CLI entry point; pinned deps via `pyproject.toml` + lockfile.
- **Container** — minimal image containing only the agent. MCP servers run as sidecars or separate processes managed by the same orchestrator.
- **Scheduling** — GitHub Actions cron, Cloud Scheduler + Cloud Run Jobs, or systemd timer — pluggable; the CLI is the invariant.
- **Environments** — `dev` and `staging` default to `email_mode: draft`. Promotion to `send` is an explicit config change reviewed in PR.

---

## 16. Module layout

```
src/pulse/
  cli.py                      # entry point: `pulse run`, `pulse backfill`
  config.py                   # loads products.yaml + pulse.yaml
  run.py                      # RunSpec, orchestration, run records
  ingestion/
    base.py                   # ReviewSource protocol, RawReview
    app_store.py              # iTunes RSS fetcher
    play_store.py             # Play Store scraper
    normalize.py              # → Review
  safety/
    scrub.py                  # PII redaction
    envelopes.py              # prompt-injection-safe wrapping
    budget.py                 # TokenBudget
  reasoning/
    embed.py
    cluster.py                # UMAP + HDBSCAN
    theme.py                  # LLM theme naming
    validate.py               # quote grounding
  rendering/
    report.py                 # Report domain object
    docops.py                 # → DocOps
    email.py                  # → HTML + text
  delivery/
    mcp_client.py             # thin MCP host/client
    docs.py                   # Docs MCP adapter
    gmail.py                  # Gmail MCP adapter
    orchestrator.py           # idempotency + retries
  storage/
    sqlite.py                 # reviews cache
    runs.py                   # run records
  observability/
    logging.py
    metrics.py
tests/
  fixtures/                   # canned reviews, recorded MCP responses
  unit/
  integration/                # full pipeline with mocked MCPs
config/
  pulse.yaml
  products.yaml
```

---

## 17. Testing strategy

| Layer         | Strategy                                                                          |
|---------------|-----------------------------------------------------------------------------------|
| Ingestion     | VCR-style recorded HTTP fixtures; assert normalization to `Review`                |
| Safety        | Golden tests for PII scrub (emails, phones, PAN, Aadhaar); injection payloads     |
| Reasoning     | Deterministic tests with frozen embeddings + stubbed LLM returning fixed JSON     |
| Validator     | Property tests: generated quotes that are/aren't substrings of source reviews     |
| Rendering     | Snapshot tests on `DocOps` and email HTML                                         |
| Delivery      | In-memory fake MCP servers implementing the tool contract; assert idempotency     |
| End-to-end    | Full pipeline with fake MCPs; re-run same `RunSpec` twice → exactly one section, one email |

---

## 18. Open extension points (explicitly deferred)

Per the non-goals in the problem statement, these are designed-for-future but not in the initial build:

- **Additional sources** — adding Twitter/Reddit is a new `ReviewSource` + scrub rules; no changes below the ingestion layer.
- **Alternative delivery** — swapping Slack for Gmail is a new MCP adapter + renderer variant; idempotency model reuses the run id.
- **Product-level trends** — week-over-week theme diffs can be computed from run records without touching ingestion.

---

## 19. Sequence — a single weekly run (happy path)

```text
Scheduler ──▶ CLI ──▶ Agent
                        │
                        ├─ Ingest (App Store + Play) ─▶ 420 raw reviews
                        ├─ Normalize + dedup          ─▶ 380 reviews
                        ├─ PII scrub                  ─▶ 380 scrubbed reviews
                        ├─ Embed (batched)            ─▶ 380 × d vectors
                        ├─ UMAP → HDBSCAN             ─▶ 7 clusters (3 kept)
                        ├─ LLM theme naming × 3       ─▶ 3 theme objects
                        ├─ Validate quotes            ─▶ 9/9 quotes grounded
                        ├─ Render DocOps + email
                        │
                        ├─ Docs MCP: find_or_create ─▶ docId
                        ├─ Docs MCP: find_heading_anchor("pulse-groww-2026-W16") ─▶ MISS
                        ├─ Docs MCP: append_section(DocOps)                     ─▶ headingId
                        ├─ Docs MCP: get_heading_link(headingId)                ─▶ deepLink
                        │
                        ├─ Gmail MCP: search("X-Pulse-Run-Id:...")              ─▶ MISS
                        ├─ Gmail MCP: send(email with deepLink, X-Pulse-* hdrs) ─▶ messageId
                        │
                        └─ Write run record → data/runs/{run_id}.json
```

A re-run of the same `RunSpec` takes the same path but hits idempotency on both MCPs and completes as a no-op with the original identifiers preserved.
