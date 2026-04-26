# Weekly Product Review Pulse

Automated weekly "pulse" that turns public App Store and Google Play reviews for **Groww** into a one-page insight report and delivers it to stakeholders through Google Workspace — using MCP (Model Context Protocol) for all writes to Google Docs and Gmail.

## What it does

Each Monday, the agent:

1. **Ingests** public reviews from the last 12 weeks (App Store + Google Play).
2. **Clusters** feedback using local embeddings (MiniLM) + UMAP + HDBSCAN.
3. **Themes** each cluster via a free-tier LLM (Groq by default; Gemini / local Ollama also supported).
4. **Validates** every quote must exist verbatim in a real review (zero fabrication).
5. **Delivers** via MCP:
   - Appends a dated section to the *Weekly Review Pulse — Groww* Google Doc.
   - Sends a stakeholder email with the top themes and a deep link back to the Doc.

**Zero cost to run** — all tools use free tiers or run locally.

---

## Quick start

### Prerequisites

- Python 3.11+ ([install via pyenv](https://github.com/pyenv/pyenv) or [python.org](https://www.python.org/downloads/))
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) — fast Python package manager

```bash
# Install uv (macOS / Linux)
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 1. Install dependencies

```bash
# Clone and enter the project
cd "Project 3 - App Reviews Insights Analyzer"

# Install core deps and dev tools
uv sync --extra dev

# Install reasoning + ingestion extras (needed from Phase 3 onward)
uv sync --all-extras
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — minimum required for Phase 3+:
#   LLM_PROVIDER=groq
#   GROQ_API_KEY=<your free key from https://console.groq.com/keys>
```

### 3. Run the CLI

```bash
# Show help
pulse --help

# Dry-run for current week (no network calls, no MCP calls)
pulse run --product groww --dry-run

# Dry-run for a specific ISO week
pulse run --product groww --iso-week 2026-W16 --dry-run

# Backfill a range of weeks (dry-run)
pulse backfill --product groww --from 2026-W10 --to 2026-W12 --dry-run

# Compare two runs (theme deltas)
pulse diff --from <run_id_a> --to <run_id_b>

# Hygiene checks (secret scan + artifact health)
pulse doctor

# Start the on-demand web portal (button triggers workflow_dispatch)
pulse portal --host 127.0.0.1 --port 8780
```

---

## Configuration

| File | Purpose |
|------|---------|
| `config/products.yaml` | Product registry (app IDs, store countries, stakeholders) |
| `config/pulse.yaml` | Runtime settings (window, budget, email mode, MCP commands) |
| `.env` | Secrets and overrides (never committed) |

### MCP server endpoint

This project is configured to use:

- Docs MCP: `https://saksham-mcp-server-uht7.onrender.com`
- Gmail MCP: `https://saksham-mcp-server-uht7.onrender.com`

Both are set in `config/pulse.yaml` with `transport: sse`.

### Delivery targets (Groww)

- Google Doc target: `https://docs.google.com/document/d/1Xvi2uEY4PePwdb8VWC2Bs9RCSFY_b1OmW0wJNUoIhK8/edit?pli=1&tab=t.0`
- Email recipient: `mukbathija@gmail.com`

### Weekly doc title policy

- Phase 6 delivery is configured to create/find a dedicated weekly doc title:
  - `Weekly Review Pulse — Groww — {iso_week}`
- This requires MCP support for doc create/find APIs.
- If your MCP only exposes `append_to_doc` with a pre-existing `doc_id`, weekly new-doc mode will fail until the MCP server adds a create/find-doc tool.

### Email mode

`pulse.yaml` defaults to `email_mode: draft`. Change to `send` when you're ready for production:

```yaml
run:
  email_mode: send
```

### Scheduling

- GitHub Actions weekly workflow: `.github/workflows/pulse.yml`
- Local alternatives: see `docs/local-scheduling.md`
- Workflow schedule is disabled; runs are trigger-only (`workflow_dispatch`) for portal-driven execution.

### On-demand portal

Use the webpage flow to trigger report generation only when requested:

1. Start portal:
   - `pulse portal --host 127.0.0.1 --port 8780`
2. Open:
   - `http://127.0.0.1:8780`
3. Click **Get latest report**:
   - Button switches to **Generating report**
   - Portal triggers GitHub Actions `workflow_dispatch`
   - On completion, it renders the current summary UI and replaces CTA text with:
     - `The detailed report has been shared to you on your email`

Required env vars for portal:

- `GITHUB_TOKEN` (PAT with actions read/write on your repo)
- `PULSE_GITHUB_REPO` (for example `owner/repo`)
- Optional: `PULSE_WORKFLOW_REF` (default `main`), `PULSE_WORKFLOW_FILE` (default `pulse.yml`)

---

## LLM providers (all free)

| Provider | Key required | Notes |
|----------|-------------|-------|
| `groq` *(default)* | `GROQ_API_KEY` | Free tier at [console.groq.com](https://console.groq.com/keys). Used for theming — only ~1 call per cluster (≤3 / run). |
| `ollama` | None | Fully local — run `ollama serve && ollama pull llama3.2:3b` |

Set `LLM_PROVIDER=ollama` in `.env` for a completely offline run.

---

## Development

```bash
# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov

# Lint
uv run ruff check src/ tests/

# Type check
uv run mypy src/pulse/

# Install pre-commit hooks
uv run pre-commit install
```

---

## Project structure

```
src/pulse/
  cli.py               # Entry point: pulse run / backfill / status
  config.py            # Product registry and pulse settings
  run.py               # RunSpec and deterministic run_id
  ingestion/           # App Store + Play Store fetchers (Phase 1)
  safety/              # PII scrub, envelopes, token budget (Phase 2)
  reasoning/           # Embed → cluster → theme → validate (Phase 3)
  rendering/           # DocOps + email rendering (Phase 4)
  delivery/            # MCP client + Docs + Gmail adapters (Phase 5)
  storage/             # SQLite cache + run records (Phase 1 / Phase 6)
  observability/       # Logging + metrics (Phase 7)
config/
  products.yaml        # Groww product definition
  pulse.yaml           # Runtime defaults
tests/
  unit/                # Fast, isolated tests
  integration/         # Multi-module tests with fakes
  fixtures/            # Recorded HTTP + MCP responses
data/                  # Local cache (git-ignored)
```

---

## Phase status

| Phase | Name | Status |
|-------|------|--------|
| 0 | Bootstrap & configuration skeleton | ✅ Done |
| 1 | Ingestion & normalization | ✅ Done |
| 2 | Safety layer (PII scrub, budget) | ✅ Done |
| 3 | Reasoning (embed → cluster → theme) | ✅ Done |
| 4 | Rendering (DocOps + email) | ✅ Done |
| 5 | MCP delivery (Docs + Gmail) | ✅ Done |
| 6 | Orchestration, CLI, scheduling | ✅ Done |
| 7 | Observability & hardening | ✅ Done |

---

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full system design.
See [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) for the phase-wise plan.
See [`EVALUATIONS.md`](EVALUATIONS.md) for per-phase test criteria.
See [`EDGE_CASES.md`](EDGE_CASES.md) for edge-case handling.
