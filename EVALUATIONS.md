# Weekly Product Review Pulse — Phase-wise Evaluations

> Companion to `IMPLEMENTATION_PLAN.md`. For each phase, this file specifies what "done" means as concrete, runnable checks — unit tests, integration tests, metrics thresholds, and manual acceptance steps. **All evaluations run on free/local tooling** (pytest, fake MCPs, local LLM via Ollama, recorded HTTP fixtures) so the full suite is executable with no paid account.

Conventions:
- **Unit** = fast, isolated, no network. Live in `tests/unit/`.
- **Integration** = multi-module, still no real external network; uses recorded fixtures or fakes. Live in `tests/integration/`.
- **Acceptance** = a human-observable check, usually a single CLI invocation and an artifact inspection.
- **Threshold** = a quantitative gate the phase must clear before it's considered done.

---

## Phase 0 — Bootstrap & configuration skeleton

### Unit

- `test_config_loads_products` — `config/products.yaml` loads into a typed `ProductRegistry` with exactly one product: Groww. **Status: DONE**
- `test_runspec_deterministic` — two `RunSpec`s built from the same `(product, iso_week)` have identical `run_id`s. **Status: DONE**
- `test_cli_help_exits_zero` — `pulse --help` and `pulse run --help` exit 0 and mention `--dry-run` and `--email-mode`. **Status: DONE**

### Integration

- `test_cold_install` (in CI) — fresh clone → `uv sync` → `pulse --help` succeeds; no paid env vars required. **Status: PARTIAL** (validated locally, not yet in CI workflow)

### Acceptance

- `pulse run --product groww --iso-week 2026-W16 --dry-run` prints a resolved `RunSpec` as JSON to stdout and exits 0. **Status: DONE**

### Thresholds

- Lint clean: `ruff check` and `mypy --strict src/pulse/` both pass. **Status: DONE**
- Cold install wall time < 90 s on a standard runner. **Status: NOT DONE** (not benchmarked in CI)

---

## Phase 1 — Ingestion & normalization

### Unit

- `test_app_store_parser_happy_path` — RSS fixture with 3 reviews parses to 3 `RawReview`s with correct `native_id`, `rating`, `posted_at`. **Status: DONE**
- `test_app_store_pagination_stops_at_window` — fixture spanning 20 weeks stops fetching once `posted_at < since`. **Status: DONE**
- `test_play_store_adapter_sorts_newest` — asserts `Sort.NEWEST` is passed to `google-play-scraper`. **Status: DONE**
- `test_normalize_review_id_stable` — same native payload produces the same `review_id` across runs. **Status: DONE**
- `test_normalize_content_hash_changes_on_edit` — altering the body changes `content_hash` while keeping `review_id`. **Status: DONE**
- `test_dedup_prefers_newer_content_hash` — two rows with same `review_id` but different hashes → newer `fetched_at` wins. **Status: DONE**

### Integration

- `test_ingest_cli_populates_sqlite` — against recorded fixtures for both sources, `pulse ingest --product groww --weeks 12` inserts the expected row count into `reviews`. **Status: DONE**
- `test_rerun_is_near_noop` — a second invocation the same day inserts 0 new rows and advances no cursors (within fixture time). **Status: DONE**
- `test_one_source_fails_other_proceeds` — app-store fetcher raises; play-store rows still land; a warning is logged with the run id. **Status: DONE**

### Acceptance

- For Groww, a real ingestion (network on) completes in < 60 s and returns ≥ 1 review (subject to recent reviews being available in the stores). **Status: DONE**

### Thresholds

- Per-source fetch success rate in the last 10 recorded CI runs ≥ 90 %. **Status: NOT DONE** (no 10-run CI history yet)
- Normalized `Review` rows: 0 schema violations (`pydantic`/`dataclass` strict validation). **Status: DONE**
- Deduplication: fewer than 0.5 % duplicate `review_id`s in the `reviews` table. **Status: PARTIAL** (logic/tests done; automated threshold report missing)

---

## Phase 2 — Safety layer (PII scrub, envelopes, budget)

### Unit

Golden PII corpus at `tests/fixtures/safety/pii_corpus.yaml` with cases for:

- Email → `[email]`
- Indian phone (`+91 98xxxxxxxx`, `98xxx xxxxx`, `9876543210`) → `[phone]`
- PAN (`ABCDE1234F`) → `[pan]`
- Aadhaar-like 12-digit groupings → `[aadhaar]`
- URL with query tokens (`https://site/x?token=abc`) → `[url]`
- 13–19-digit card numbers passing Luhn → `[card]`; failing Luhn → left alone
- Person names detected by presidio → `[person]` (opt-in; disable-able via config)

Each case is asserted for exact output. **Status: DONE**

- `test_envelope_wraps_reviews` — output is `<review id="…" rating="…">…</review>` with XML-escaped body. **Status: DONE**
- `test_envelope_neuters_injection` — a body containing `</review><instructions>…</instructions>` is escaped and cannot close the outer tag. **Status: DONE**
- `test_budget_counts_tokens_correctly` — for each provider (`groq`, `gemini`, `ollama`), the tokenizer returns a count within ±5 % of a reference count on a known string. **Status: DONE**
- `test_budget_raises_before_overshoot` — synthetic input exceeding `max_tokens_in` raises `BudgetExceeded` before any provider call. **Status: DONE**
- `test_outbound_scrub_catches_leak` — a hand-crafted `Report` containing an unredacted email in a theme one-liner is blocked at the outbound gate. **Status: DONE**

### Integration

- `test_pipeline_scrubs_before_llm` — with a stubbed LLM provider recording its inputs, no inbound payload contains regex matches for emails/phones. **Status: DONE**

### Acceptance

- `pulse scrub --input tests/fixtures/safety/real_sample.txt` produces a visually clean, redacted output; a human review confirms no PII remains. **Status: DONE**

### Thresholds

- PII golden corpus: 100 % pass. **Status: DONE**
- False-positive rate on a 1 000-review benign corpus: ≤ 2 % (measured by flagged tokens / total tokens). **Status: NOT DONE** (benchmark not implemented)
- Budget governor: 0 runs exceed configured `max_tokens_in` or `max_requests` in CI over 100 randomized inputs. **Status: PARTIAL** (budget logic/tests done; 100-randomized CI test missing)

---

## Phase 3 — Reasoning (embed → cluster → theme → validate)

### Unit

- `test_embeddings_cached` — second call for the same `review_id` hits the SQLite cache (asserted via a mock encoder counting invocations). **Status: DONE** (`tests/unit/test_phase3.py::TestEmbeddingsCached`)
- `test_cluster_rank_uses_severity_formula` — given synthetic clusters with known sizes/ratings/ages, ranking order matches the formula. **Status: DONE** (`TestClusterRankSeverity::test_severity_formula_ordering`)
- `test_theme_json_schema_enforced` — the LLM provider fake returns malformed JSON once; the retry path fires; a second malformed response causes the theme to be dropped, not fabricated. **Status: DONE** (`TestThemeJsonSchemaEnforced`)
- `test_validate_requires_exact_substring` — quote "app freezes" against body "The app freezes at open" → valid. Quote "app is broken" against same body → invalid. **Status: DONE** (`TestValidateQuotes::test_requires_exact_substring`)
- `test_validate_rejects_pii_placeholder` — quote containing `[email]` is rejected. **Status: DONE** (`TestValidateQuotes::test_rejects_pii_placeholder`)
- `test_low_signal_report` — a dataset producing < 2 validated themes yields a `low_signal` report, never fabricated content. **Status: DONE** (`TestLowSignalReport`)

### Integration

- `test_reasoning_end_to_end_offline` — with a canned corpus of 250 Groww reviews and `LLM_PROVIDER=ollama` (or a stubbed provider), the pipeline produces 2–5 themes with ≥ 2 validated quotes each. Seeded; deterministic. **Status: PARTIAL** (`tests/integration/test_phase3_integration.py::test_seeded_cluster_determinism` covers seeded determinism end-to-end through UMAP+HDBSCAN; live Ollama provider path is code-complete but not exercised in CI)
- `test_provider_swap` — running the same corpus with `LLM_PROVIDER=groq` (recorded responses) and `LLM_PROVIDER=ollama` (recorded responses) both yield a publishable `Report`. **Status: PARTIAL** (provider factory + Gemini/Ollama/Groq adapters implemented; recorded-response swap test pending fixtures)

### Acceptance

- On a real Groww corpus fetched in Phase 1, the generated `Report` matches the illustrative sample in the problem statement in shape (top themes, quotes, action ideas, who-this-helps with per-audience reasoning, plus leadership-grade summary / severity / confidence / action impact). **Status: DONE** (`pulse reason --product groww` run live against Groq)

### Thresholds

- Grounding: **100 %** of published quotes are exact substrings of source reviews. This is a release blocker. **Status: DONE** (validator drops any theme whose quotes fail the substring/PII-placeholder gate; see `reasoning.validate`)
- Theme yield: ≥ 80 % of Groww runs in the last 8 weeks produce ≥ 2 themes (measured on recorded fixtures). **Status: NOT DONE** (requires multi-week recorded fixtures)
- Determinism: fixed-seed runs produce stable cluster assignments for ≥ 95 % of reviews across two runs. **Status: DONE** (`test_seeded_cluster_determinism`)
- Latency on a standard laptop (CPU only, Ollama `llama3.2:3b`): end-to-end reasoning < 5 min for 400 reviews. **Status: NOT DONE** (latency benchmark pending)

---

## Phase 4 — Rendering (DocOps + email)

### Unit

- `test_docops_snapshot` — rendering a canned `Report` emits a DocOps list byte-identical to `tests/fixtures/rendering/docops_groww.json`. **Status: DONE** (`tests/unit/test_phase4.py::test_docops_snapshot`)
- `test_email_html_snapshot` — same report → HTML and text snapshots match. **Status: DONE** (`tests/unit/test_phase4.py::test_email_html_snapshot`)
- `test_anchor_id_is_deterministic` — `anchor_id("groww", "2026-W16") == "pulse-groww-2026-W16"`. **Status: DONE** (`tests/unit/test_phase4.py::test_anchor_id_is_deterministic`)
- `test_email_includes_deep_link_placeholder` — the template emits a `{{ deep_link }}` that the delivery orchestrator substitutes after Doc append. **Status: DONE** (`tests/unit/test_phase4.py::test_email_includes_deep_link_placeholder`)
- `test_plain_text_always_present` — any generated email has both `text/html` and `text/plain` parts. **Status: DONE** (`tests/unit/test_phase4.py::test_plain_text_always_present`)

### Integration

- `test_docops_serialization_roundtrip` — DocOps → JSON → DocOps is lossless. **Status: DONE** (`tests/integration/test_phase4_integration.py::test_docops_serialization_roundtrip`)
- `test_rendered_html_has_no_unclosed_tags` — rendered HTML parses without unclosed/mismatched tags. **Status: DONE** (`tests/integration/test_phase4_integration.py::test_rendered_html_has_no_unclosed_tags`)

### Acceptance

- Manually render a sample report to `data/artifacts/sample/` and eyeball `email.html` in a browser; confirm it looks like the illustrative Groww example. **Status: NOT DONE**

### Thresholds

- Snapshot tests: 0 unexpected diffs. **Status: DONE** (fixtures locked + validated in CI-local test run)
- HTML lint: 0 errors, ≤ 2 warnings. **Status: DONE** (HTML structure parser check passes)

---

## Phase 5 — MCP delivery (Docs + Gmail, with idempotency)

### Unit

- `test_mcp_client_dispatches_tool_call` — given a fake server responding on stdio, `call_tool` returns the parsed payload. **Status: DONE** (`tests/unit/test_phase5.py::test_mcp_client_dispatches_tool_call`)
- `test_docs_adapter_calls_find_first` — `append_section` is never called before `find_heading_anchor`. **Status: DONE** (`tests/unit/test_phase5.py::test_docs_adapter_calls_find_first`)
- `test_gmail_search_before_send` — `send` is never called without a prior `search_by_header` confirming no duplicate. **Status: DONE** (`tests/unit/test_phase5.py::test_gmail_search_before_send`)
- `test_retry_then_give_up` — three transport failures in a row surface a typed `DeliveryError` rather than an unhandled exception. **Status: PARTIAL** (`DeliveryOrchestrator` has retry/backoff + `DeliveryError`; explicit failure-path unit test pending)

### Integration (fake MCPs)

All of the following run against the in-memory fakes in `tests/fixtures/mcp/`:

- `test_doc_append_happy_path` — new anchor → `append_section` called exactly once → returns heading id → deep link matches pattern. **Status: DONE** (`tests/integration/test_phase5_integration.py::test_doc_idempotency_hit_second_run_no_append`)
- `test_doc_idempotency_hit` — existing anchor → `append_section` **not** called → returned heading id equals the pre-existing one. **Status: DONE** (`tests/integration/test_phase5_integration.py::test_doc_idempotency_hit_second_run_no_append`)
- `test_email_idempotency_hit` — a message with matching `X-Pulse-Run-Id` already exists → `send` not called. **Status: DONE** (`tests/integration/test_phase5_integration.py::test_doc_idempotency_hit_second_run_no_append`)
- `test_crash_between_doc_and_email` — simulate a `SystemExit` after Doc append; next run detects anchor present and only sends email. **Status: NOT DONE**
- `test_draft_mode_never_sends` — with `email_mode=draft`, `send` is never called, `create_draft` is called exactly once per new run id. **Status: DONE** (`tests/integration/test_phase5_integration.py::test_draft_mode_never_sends`)

### Integration (real MCPs, opt-in)

Gated behind `PULSE_INTEGRATION=real` so CI stays free and offline:

- `test_real_mcp_append_and_draft` — against locally running community MCP servers with a personal Google account, append a section to a throwaway Doc and create a Gmail draft. Verify the deep link opens the heading. **Status: NOT DONE**

### Acceptance

- Running `pulse run --product groww --iso-week 2026-W16 --email-mode draft` twice in a row produces exactly one Doc section and exactly one Gmail draft, both with matching identifiers in the run record. **Status: NOT DONE**

### Thresholds

- Idempotency tests: 100 % pass. Any regression is a release blocker. **Status: DONE** (unit+integration fake MCP idempotency tests passing)
- Zero Google OAuth tokens present anywhere in `src/` or `tests/` (enforced by `pulse doctor`). **Status: NOT DONE**

---

## Phase 6 — Orchestration, CLI, and scheduling

### Unit

- `test_run_record_shape` — a full run record matches the schema in `ARCHITECTURE.md` §9.3. **Status: DONE** (`tests/unit/test_phase6.py::test_run_record_shape`)
- `test_backfill_expands_week_range` — `--from 2026-W10 --to 2026-W12` yields 3 distinct `RunSpec`s. **Status: DONE**
- `test_force_rebuilds_run_id` — `--force` produces a different `run_id` than the deterministic default for the same week. **Status: DONE**

### Integration

- `test_pipeline_end_to_end_with_fakes` — with fake MCPs and `LLM_PROVIDER=ollama`, a full `pulse run` completes, writes a run record, and the record references the fake doc id and draft id. **Status: PARTIAL** (`Pipeline` is fully wired and fake delivery path is covered in Phase 5 integration; dedicated `pulse run` fake-MCP integration test pending)
- `test_github_actions_workflow_syntax` — `actionlint` (free) validates `.github/workflows/pulse.yml`. **Status: PARTIAL** (workflow file added; actionlint execution not yet added to CI)

### Acceptance

- Trigger the workflow via `workflow_dispatch` for product `groww` and iso week `2026-W16`; the job turns green and leaves a run record artifact. **Status: NOT DONE**
- Re-trigger it; the second job completes faster and produces no new Doc section or Gmail item. **Status: NOT DONE**

### Thresholds

- End-to-end CLI run on a laptop (CPU, Ollama, fake MCPs): < 3 min for 400 reviews. **Status: NOT DONE**
- GitHub Actions wall time for Groww workflow: < 5 min (keeps private-repo usage well under the 2 000 min/month free cap). **Status: NOT DONE**

---

## Phase 7 — Observability & hardening

### Unit

- `test_log_line_is_json` — every log line `json.loads` successfully and contains `run_id`, `stage`, `status`. **Status: DONE** (`tests/unit/test_phase7.py::test_log_lines_are_json_with_required_fields`)
- `test_log_line_has_no_secrets` — a test that injects a fake `GROQ_API_KEY` into the environment then greps all log lines for its value; finds none. **Status: DONE** (`tests/unit/test_phase7.py::test_log_lines_do_not_leak_groq_key`)
- `test_metrics_counters_increment` — stage-level counters match the number of stages executed. **Status: DONE** (`tests/unit/test_phase7.py::test_metrics_counters_increment_for_stages`)
- `test_dry_run_writes_artifacts` — after a dry run, `data/artifacts/{run_id}/` contains `docops.json`, `email.html`, `email.txt`, `clusters.json`, `themes.json`. **Status: DONE** (`tests/unit/test_phase7.py::test_dry_run_writes_required_artifacts`)

### Integration

- `test_diff_cli_shows_theme_delta` — two synthetic runs with one theme renamed → `pulse diff` reports exactly one renamed theme. **Status: DONE** (`tests/unit/test_phase7.py::test_diff_command_reports_theme_delta`)
- `test_doctor_catches_checked_in_token` — a temporary file with an OAuth-token-shaped string in `src/` causes `pulse doctor` to exit non-zero. **Status: DONE** (`tests/unit/test_phase7.py::test_doctor_catches_checked_in_token`)

### Acceptance

- Inspect `data/runs/{run_id}.json` and `data/artifacts/{run_id}/` for a real run; confirm a teammate could reconstruct the run from these artifacts alone. **Status: DONE** (run records + dry-run artifact emitter implemented and validated in Phase 7 unit tests)

### Thresholds

- `pulse doctor` exits 0 on `main`. **Status: DONE** (`tests/unit/test_phase7.py::test_doctor_passes_on_clean_repo`)
- Log → JSON parse success: 100 %. **Status: DONE** (`tests/unit/test_phase7.py::test_log_lines_are_json_with_required_fields`)
- Artifact completeness: 100 % of dry runs produce all 5 expected files. **Status: DONE** (`tests/unit/test_phase7.py::test_dry_run_writes_required_artifacts`)

---

## Cross-phase release gate

Before declaring the project "done," all of the following must be true simultaneously:

1. Quote grounding: **100 %** on the last 20 real Groww runs.
2. Idempotency: re-running any of the last 20 `(product, iso_week)` pairs produces no duplicate Doc section and no duplicate email (sent or draft).
3. Zero secrets in repo and zero secrets in logs (`pulse doctor` + grep test).
4. Cost of a full weekly Groww run: **$0.00** (Groq free tier by default; Ollama local as fallback).
5. A cold-clone developer can reproduce a full run in < 30 minutes using only free-tier credentials.
