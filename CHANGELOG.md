# Changelog

All notable changes to Lummenna will be documented in this file.

> Note: the `datasets/` directory referenced by older entries was renamed to
> `seed_datasets/` (it shadowed the HuggingFace `datasets` package and broke
> MS MARCO/BEIR loading). Use `python -m seed_datasets.seed` and
> `seed_datasets/domain_corpus.json`.

## [Unreleased]

## [1.0.1rc1] (prepared 2026-09-04, pending website deployment)

### Added
- Community distribution through PyPI and lummenna.com as a versioned wheel and
  corresponding source archive, with published SHA-256 checksums.
- A checksum-pinned Community sidecar Dockerfile that requires API
  authentication and runs as a non-root user.
- A public Community deployment page that requires no source repository access.
- **Durable replay-nonce store for the Beam P2P transport** (Pro, `advanced_p2p`).
  `LUMMENNA_P2P_REPLAY_STORE=persistent` keeps the set of already-accepted frame
  nonces in the Lummenna database (`lummenna/p2p/replay.py`, schema migration
  0006) instead of in process memory, so a restart no longer empties it. Until
  now a frame captured shortly before a restart could be replayed once
  afterwards, for whatever remained of its timestamp window. Uniqueness is
  decided by a SQLite primary-key conflict rather than a read-then-write, so
  concurrent deliveries of the same frame — including from separate processes —
  cannot both be accepted.
  - Retention is exact rather than heuristic: a nonce is kept until
    `frame_timestamp + LUMMENNA_P2P_REPLAY_WINDOW_SECONDS`, past which the
    timestamp check rejects the frame regardless. Expired rows are swept on an
    amortised schedule, off the per-frame path.
  - `LUMMENNA_P2P_REPLAY_MAX_ENTRIES` (default 100,000) caps the set under flood,
    evicting nearest-expiry first and logging at WARNING.
  - Fail-closed: if the store cannot be consulted, the frame is rejected and the
    failure is logged at ERROR. Replay checking is never skipped.
  - Community is unaffected and ungated: the default remains the process-local
    store, whose restart limitation is documented in SECURITY.md.
- `LUMMENNA_P2P_REPLAY_WINDOW_SECONDS` now makes the Beam replay window
  configurable; it was previously fixed at 30 seconds and unreachable from config.

### Changed
- **BREAKING (read scope): `tenant_id="default"` is now an ordinary tenant, not
  an administrator wildcard.** `SearchPipeline._filter_by_tenant` previously
  returned early whenever the caller was the default tenant, so a default-scoped
  search skipped the ownership check entirely and could return chunks belonging
  to *every* tenant. It now validates every result against `chunk.tenant_id` for
  the default scope too, and only rows genuinely owned by `default` are
  returned. The licence gate is unchanged: `default` remains ungated for
  Community, and a named tenant still requires the `multi_tenant` Pro feature.
  Single-tenant deployments see no difference, because in them every row already
  carries `default`. Callers that relied on the old behaviour to read across
  tenants were relying on a tenant-isolation defect.
- The repair loop now preserves tenant scope. `SearchRepair.attempt_repair`
  takes `tenant_id` and forwards it to the retried `execute` call. Previously the
  retry dropped the scope and fell back to `default`, so a failed search inside a
  named tenant could return another tenant's chunks on its second attempt.
- The replay-nonce set moved behind a `ReplayNonceStore` interface with two
  implementations (`InMemoryNonceStore`, `SqliteNonceStore`). `P2PSign` takes a
  `nonce_store` argument in place of `max_seen_nonces`, and its process-local
  ceiling default rose from 10,000 to 100,000 now that it is configurable.
- A replayed or malformed Beam frame is now logged distinctly from an internal
  handler error, and a replay-store outage is logged at ERROR rather than being
  absorbed into a generic warning.

### Fixed
- `BeamNode.rotate_key` no longer discards the P2P replay-nonce set. It rebuilt
  the signer from scratch, which silently cleared every recorded nonce and
  reopened the replay window for frames still inside it. The signer is now
  rekeyed in place (`P2PSign.rekey`), which is sound because a nonce is derived
  from the frame's HMAC and is therefore already bound to the key that produced
  it — nonces recorded under the old key cannot collide with new-key nonces.
- Replay-nonce eviction is now expiry-ordered. The previous cap rebuilt the set
  from `set(list(...)[-5000:])`; `set` iteration order is hash-dependent, so it
  could discard freshly-recorded nonces while retaining already-expired ones.
  Nonces are now retired once their frame falls outside the replay window — past
  that point the timestamp check rejects the frame regardless — and the ceiling
  only binds under flood, evicting nearest-expiry first.

## [1.0.0] (prepared 2026-08-07, NOT RELEASED)

### Overview

> **This version was prepared but never released.** No `v1.0.0` tag exists, no
> distribution was published to PyPI, and no container image was pushed. The
> entries below record the work done under that version number in
> `pyproject.toml`; they do not record a shipped release. Treat this section as
> the contents of the pending first release, not as history.

Lummenna v1.0.0 consolidates the beta-phase development into a documented,
benchmark-validated, integration-ready system.

### Added
- Consolidated introductory whitepaper. Held internally and not tracked in this
  repository; `docs/` is excluded because it holds commercially sensitive
  material. Request it directly rather than looking for it in the tree.
- Unified version numbering across source code, documentation, and APIs.

### Changed
- Version number advanced from `0.2.0-beta` to `1.0.0` to denote feature
  completeness of the intended first release. This is a version-string change
  only; it does not assert that a release was published or that the API has been
  held stable, since neither version was ever tagged or distributed.
- Version bumped from `0.2.0-beta` to `1.0.0` across `pyproject.toml`, package `__version__`, API server, MCP server, and all documentation.

### Known Limitations (from v0.2.0-beta, still applicable)
- Encryption-at-rest partially implemented — SQLCipher schema support exists; use OS-level disk encryption as stopgap.
- Full-corpus BEIR leaderboard submission remains future work.
- P2P Beam protocol is hardened for trusted LANs but not adversarial WANs.

## [0.2.0-beta] — 2026-08-05

### Added
- Domain corpus dataset (`datasets/domain_corpus.json`) — a hand-crafted knowledge corpus with 111 fact chunks across 8 rooms (machine_learning, nlp, cybersecurity, distributed_systems, healthcare_ai, quantum_computing, open_source, climate_tech), each with 4 structured loci. Designed for both palace seeding (`python -m datasets.seed`) and benchmarking.
- Palace seed script (`datasets/seed.py`) populates a Lummenna instance from the domain corpus JSON with real embeddings, creating a populated, queryable knowledge base in ~2 seconds.
- Palace Navigation Efficiency (PNE) benchmark (`benchmarks/navigation/`) — proprietary evaluation suite measuring latency speedup, recall retention, intent-routing accuracy, and pruning efficiency of Lummenna's room/locus topology vs. flat global search. Outputs JSON + Markdown.
- End-to-End Memory Quality benchmark (`benchmarks/e2e/`) — multi-turn conversational memory persistence benchmark with 7 agent personas, 28 queries across 7 sessions. Measures R@k recall, similarity scores, and per-query latency using embedding-similarity self-grading (no LLM judge, no comparison against MemGPT/Mem0/Zep).
- LangGraph integration (`lummenna.integrations.langgraph`) provides `LummennaCheckpointSaver` for graph state persistence and `LummennaGraphStore` for cross-thread long-term memory. Installable via `pip install -e '.[langgraph]'`. Includes 9 test cases.
- Centralized logging helper (`lummenna.logging`) — `get_console_logger()` with structlog preference and stdlib `logging` fallback, eliminating the project-wide `except Exception: pass` anti-pattern from 32 modules.
- API versioning (`/v1/` prefix) for all operational endpoints with backward-compatible public paths (`/health`, `/dashboard`, `/metrics`).
- Subresource Integrity (SRI) hash for Chart.js CDN resource in dashboard HTML.
- PGP key for `security@lummenna.ai` published in `SECURITY.md`.
- Optical degradation benchmark (`benchmarks/optical/run.py`) — evaluates retrieval quality at FP32, FP16, INT8, and BINARY quantization levels.
- TFC sensitivity benchmark (`benchmarks/tfc/run.py`) — grid search across Twin-Force Controller parameters.
- Stress test benchmark (`benchmarks/stress/run.py`) — 100k+ chunk ingestion, multi-threaded query throughput, and L3 budget eviction validation.

### Changed
- Roadmap in README updated to reflect M1–M3 completion and M4 in-progress status.
- Full codebase reformatted with `ruff format` for consistent style.
- `benchmarks/run_all.py` now includes the `navigation` and `e2e` suites in the unified orchestrator.
- Dashboard retrieval metrics (`mcp_server.py`) now load benchmark values dynamically from JSON result files instead of hardcoded constants.
- Upgraded from alpha to beta status with API stability commitment.
- `SECURITY.md` updated with supported versions, PGP key, and beta-period limitations.

### Fixed
- **CRITICAL**: USearch vector backend now gracefully falls back to brute-force (FakeSqliteVecBackend) when the `usearch` package is unavailable, instead of crashing with `NotImplementedError`.
- **CRITICAL**: `SqliteVecBackend.degrade()` no longer silently swallows quantization failures — errors are logged before fallback removal.
- **CRITICAL**: `USearchBackend.degrade()` now properly logs warnings and removes vectors rather than silently doing nothing before removal.
- L2 interference checking (`forgetting_l2_interference.py`) now reads embeddings from `vec_chunks` when `vec_fallback` is empty, fixing a blind spot for sqlite-vec-only deployments.
- API `/turn` endpoint (`server.py`) now resolves real `room_name`, `locus_name`, `vm_score`, and `age_hours` from the database instead of using hardcoded placeholder values.
- LangGraph `alist()` async method now yields items as a proper async generator instead of returning a sync Iterator.
- Removed trailing whitespace in `lummenna/api/server.py` docstring.
- Removed unused `os` import in `lummenna/api/server.py`.
- Replaced unused local variable assignments with `_` in `tests/test_beam.py` and `tests/test_core.py`.
- Sorted and cleaned import blocks across multiple test files (auto-fixed by ruff).
- Removed dead `pass` in `benchmarks/beir/run.py` except block.
- Removed empty `if TYPE_CHECKING: pass` block in `lummenna/curiosity.py`.
- Fixed FTS5 lexical search failing on apostrophes, periods, and other punctuation characters in query strings (`retrieval_lexical.py`).
- Fixed benchmark scripts failing when `_enforce_permissions` attempted to chmod `/tmp` by ensuring store paths are nested under temp directories.

## [0.1.0] — 2026-07-20

### Added — Milestone 1 (Store & Retrieve)
- A1: SQLite Palace Schema with WAL mode
- A2: BM25 lexical channel (FTS5 bridge)  
- A3: Vector channel (sqlite-vec + USearch adapter)
- A4: FRQAD distance metric
- A5: Provenance tracking
- A6: Write path (store_memory) with dedup + interference
- A7: Interference-based forgetting (L2)
- A8: Ebbinghaus passive decay (L1)
- A9: V(m) value model with learnable weights
- A10: Budget-curated eviction (L3) with optical degradation chain
- A11: Consolidation pass runner
- B1: Intent router
- B2: Mock/ONNX embedder with local model support
- B3: Context assembly with Jinja2 templates + token budget
- C1: Palace onboarding wizard (Illuminate)
- C2: Multi-channel RRF fusion engine with FRQAD rerank
- C3: Epistemic state tracker
- C4: Goal tree with SQLite persistence
- C5: Twin-Force Controller (TFC)
- C6: Sleep-phase consolidation scheduler
- C7: Conversation memory with search→store→feedback loop
- C8: Search repair loop (self-healing)
- C9: Full Typer CLI
- C10: P2P memory sharing (Beam protocol)
- D1: Pydantic-settings based config with device profiles
- D2: FastAPI server with search/store/feedback/assemble/turn endpoints
- D3: Feedback log schema
- D8: Model download & ONNX export pipeline
- D12: Search repair with feedback-driven widening
- D13: Wear-aware batch writer for SD/eMMC endurance
- LangChain integration (`LummennaChatMemory` + `LummennaStore`)
- 24 test suites with 177 tests
- Retrieval, forgetting, and performance benchmarks
- GitHub Actions CI workflow (lint, typecheck, test, coverage)
- Dockerfile + docker-compose for production deployment

### Fixed — Production Blocker Resolution
- Lazy imports for apscheduler/zeroconf so CLI loads without optional deps
- BM25 retrieval benchmark pid/chunk_id mismatch (was returning 0 recall)
- Forgetting eviction optical_level overflow that permanently hid chunks from the release cycle
- 9 silent `except Exception: pass` blocks replaced with structured logging
- MockEmbedder silent fallback replaced with logged ModelNotAvailableError in production paths
- 59 uncommitted files committed to create a clean reproducible state

### Added — Enterprise Readiness
- Apache-2.0 LICENSE file
- pyproject.toml metadata (license, authors, classifiers, URLs, readme)
- API key authentication middleware with configurable `LUMEN_API_KEY`
- Rate limiting via slowapi with configurable `LUMEN_API_RATE_LIMIT`
- CORS middleware with configurable `LUMEN_ALLOWED_ORIGINS`
- Request ID header propagation (`X-Request-ID`)
- Request body size limit (configurable, default 1 MB)
- Proper pydantic-settings TOML config file support
- TurnRequest Pydantic model replacing bare `dict` endpoint
- Global unhandled exception handler (no stack traces in responses)
- Structured logging for all API operations
- Health check endpoint improved with proper 503 handling
- Coverage measurement (pytest-cov, 75% baseline, 50% CI threshold)
- pre-commit config (ruff + ruff-format + general hooks)
- `.dockerignore` for efficient Docker builds
- Healthcheck instructions in Dockerfile and docker-compose

### Changed
- `LummennaConfig.model_post_init` replaced with explicit `resolve_device_defaults()`
- `FallbackEmbedder` is now `MockEmbedder` with a compatibility alias
- `budget_curated_eviction` no longer increments `optical_level` per degradation step
- API server `_state` dict access guarded with null checks
- `/turn` endpoint upgraded from bare `req: dict` to validated `TurnRequest` model
