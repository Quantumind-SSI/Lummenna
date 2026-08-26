# Lummenna — Technical Documentation

**Version:** v1.0.0
**License:** AGPL-3.0-or-later (Community) / Commercial (Pro) — see `LICENSE`
**Python:** 3.10+

---

## 1. Overview

Lummenna is a local-first, self-contained memory and context framework for AI agents. It provides persistent storage, hybrid retrieval, and managed memory lifecycle for LLM-powered applications. All operations run entirely on-device — no cloud dependencies, no external APIs, no telemetry.

### Core Capabilities

| Capability | Status | Details |
|---|---|---|
| Memory storage | Working | SQLite with WAL, FTS5, bi-temporal schema |
| Hybrid retrieval | Working | BM25 (FTS5) + cosine-similarity vector + optional graph traversal with reciprocal rank fusion |
| Context assembly | Working | Jinja2 templating with token budget enforcement |
| Managed forgetting | Working | Three-layer: Ebbinghaus decay (L1), similarity interference (L2), budget eviction (L3) |
| PII detection | Working | Regex scanning (email, SSN, phone, credit card, API key, IP) with block/redact/hash strategies |
| Audit logging | Working | SQLite audit_log table with structured entries |
| Conversation tracking | Working | Turn storage, implicit feedback, context assembly |
| REST API | Working | FastAPI with auth (header key comparison), rate limiting, CORS |
| MCP server | Working | 7 tools: search, store, assemble, turn, feedback, status, dashboard |
| LangChain adapter | Working | LummennaChatMemory (requires `langchain` package) |
| LangGraph adapter | Working | LummennaCheckpointSaver (requires `langgraph` package) |
| Dashboard | Working | Real-time palace topology, TFC state, memory health (http://localhost:8848/dashboard) |
| P2P sharing | Working (LAN) | Beam protocol with AES-256-GCM + HMAC-SHA256; blocked by default (sovereign mode) |
| Encryption-at-rest | Partially implemented | SQLCipher schema support implemented; tests skipped pending v0.3.0. Use OS-level encryption as stopgap. |

### Not Yet Implemented

- Transport encryption for P2P beam
- `lummenna.api.backup` CLI command
- Per-user adaptive decay (schema column exists, not wired)

---

## 2. Architecture

```
User Input → IntentRouter → Parallel Retrieval (BM25 + Dense + Graph)
                                  │
                                  ▼
                          RRF Fusion × V(m) × FRQAD/cosine × Recency
                                  │
                                  ▼
                          Context Assembly (Jinja2)
                                  │
                                  ▼
                    Sleep Consolidation → Decay → Interference → Eviction
```

### Components

| Layer | Module | Purpose |
|---|---|---|
| Storage | `lummenna/data/schema.py` | SQLite schema, connection management, permissions |
| Retrieval | `lummenna/search.py` | Orchestrates BM25, dense, graph channels |
| Retrieval | `lummenna/force/mnemonic/retrieval_lexical.py` | FTS5 full-text search |
| Retrieval | `lummenna/force/mnemonic/retrieval_dense.py` | Vector similarity with sqlite-vec or usearch |
| Retrieval | `lummenna/force/mnemonic/retrieval_graph.py` | NetworkX-based graph traversal |
| Fusion | `lummenna/fusion.py` | RRF fusion with V(m), FRQAD/cosine, recency boost |
| Memory | `lummenna/force/mnemonic/store.py` | Chunk insertion with PII scanning, dedup, provenance |
| Memory | `lummenna/force/mnemonic/consolidation.py` | Background consolidation with dedup merging |
| Forgetting | `lummenna/force/mnemonic/forgetting_l1_decay.py` | Ebbinghaus exponential decay |
| Forgetting | `lummenna/force/mnemonic/forgetting_l2_interference.py` | Similarity-based interference weakening |
| Forgetting | `lummenna/force/mnemonic/forgetting_l3_budget.py` | Budget-curated eviction |
| Context | `lummenna/force/contextual/assembly.py` | Context assembly with token budget |
| Context | `lummenna/force/contextual/embed.py` | ONNX embedding with real/mock fallback |
| Context | `lummenna/force/contextual/token_budget.py` | Token counting |
| Controller | `lummenna/controller.py` | Twin-Force Controller (mnemonic bias α / attention temperature τ) |
| Repair | `lummenna/repair.py` | Self-healing retrieval with TFC-driven re-query |
| Compliance | `lummenna/compliance/safety_forgetting.py` | PII detection, safety-triggered deletion |
| Audit | `lummenna/audit.py` | SQLite audit event logging |
| Scheduler | `lummenna/sleep.py` | Background consolidation, decay, eviction, curiosity |
| P2P | `lummenna/p2p/beam.py` | LAN-scoped plaintext memory sharing |
| API | `lummenna/api/server.py` | FastAPI REST API with auth, rate limiting |
| CLI | `lummenna/cli/main.py` | Typer CLI for init, store, retrieve, status, daemon |
| Integrations | `lummenna/integrations/` | MCP server, LangChain, LangGraph adapters |

---

## 3. Memory Palace Data Model

The memory palace organizes data in a structured hierarchy:

```
Room (domain/project/person/ephemeral)
 └── Locus (sub-location within a room)
      └── Chunk (individual memory unit)
           ├── Content (raw text)
           ├── V(m) score (dynamic value model scalar)
           ├── Resolution (FP32 → FP16 → INT8 → BINARY → RELEASED)
           ├── Provenance chain (source tracking)
           └── Embedding vector
```

### Key Tables

| Table | Purpose |
|---|---|
| `room` | Top-level organizational unit |
| `locus` | Sub-location within a room |
| `chunk` | Individual memory with bi-temporal tracking, V(m), resolution |
| `provenance` | Source chain (user_input, agent_reasoning, consolidation, import, p2p_share) |
| `feedback_log` | Explicit/implicit/repair feedback for value model training |
| `audit_log` | Structured security/compliance event log |
| `user_profile` | Per-user preferences, goals, decay parameters |
| `goals` | Active goal tree for context assembly weighting |
| `epistemic_state` | What the system knows/assumes/establishes |

---

## 4. Retrieval Pipeline

### Channels

1. **Lexical (BM25):** SQLite FTS5 with porter stemming and unicode tokenization
2. **Dense (Vector):** Cosine similarity via sqlite-vec or usearch, with ONNX embeddings
3. **Graph:** NetworkX-based traversal from seed chunks via co-occurrence edges

### Fusion

Reciprocal Rank Fusion (k=60) weighted by:
- V(m) scalar — learned memory value
- FRQAD or cosine similarity — quantization-aware or raw vector distance
- Recency boost — `exp(-age_hours / 168)`, an exponential decay with a
  168-hour (7-day) time constant τ (at τ the boost is 1/e ≈ 0.368, not a
  half-life)
- Goal relevance — keyword match bonus

### Repair

When retrieval yields no results, the repair module:
1. Checks if TFC state suggests exploration mode (high α)
2. Re-queries with relaxed parameters
3. Falls back to broader lexical search

---

## 5. Memory Lifecycle

### L1: Ebbinghaus Decay
- Passive exponential decay: `R(t) = e^(-t / (h × ln(2)))`
- Default half-life `h = 7` days
- Configurable via `user_profile.ebbinghaus_half_life_days`
- Chunks below `vm_score < 0.05` are queued for release (logical deletion)

### L2: Similarity Interference
- When new chunks are stored, checks for similarity-conflict within the same locus
- High-similarity chunks with lower V(m) scores are weakened

### L3: Budget Eviction
- Triggers when active chunk count approaches `memory_limit_mb` budget
- Evicts lowest-V(m) chunks, prioritizing those with low access counts
- Batched writes via `WearAwareBatcher` for SD/eMMC endurance

### L4: Safety Forgetting (Manual)
- Triggered via API endpoint or `lummenna compliance audit`
- Permanently deletes chunks with provenance-chain clearance
- Writes audit events before mutation

---

## 6. Configuration

All configuration via environment variables (`LUMMENNA_` prefix), TOML file (`~/.lummenna/config.toml`), or programmatic `LummennaConfig()`.

### Key Settings

| Setting | Default | Description |
|---|---|---|
| `device` | `generic` | Device profile: rpi5, jetson-orin, orange-pi, generic |
| `context_budget` | 2048 | Token budget for context assembly |
| `memory_limit_mb` | 300 | Soft memory limit for budget eviction |
| `embedding_model` | `bge-small-en-v1.5` | ONNX embedding model |
| `embedding_dims` | 384 | Embedding vector dimensions |
| `vector_index` | `sqlite-vec` | Vector backend: sqlite-vec or usearch |
| `enable_frqad` | `true` | Use FRQAD for vector distance (fallback: cosine) |
| `enable_local_llm` | `false` | Use local LLM for narrative generation |
| `sovereign` | `true` | Block external network calls, P2P, model downloads |
| `pii_detection_enabled` | `true` | Scan stored content for PII |
| `pii_redaction_mode` | `redact` | PII handling: block, redact, or hash |
| `store_path` | `~/.lummenna/store` | Database storage path |
| `model_path` | `~/.lummenna/models` | Embedding model cache |
| `api_host` / `api_port` | `127.0.0.1` / `8848` | API server binding (loopback by default; set `LUMMENNA_API_HOST=0.0.0.0` to expose on the LAN, e.g. in Docker) |
| `api_key` | None (disabled) | Optional API authentication |
| `api_rate_limit` | `60/minute` | Rate limiting |

---

## 7. API Reference

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Liveness probe (unversioned) |
| GET | `/dashboard` | Real-time memory palace dashboard (HTML) |
| GET | `/metrics` | Machine-readable metrics for monitoring |
| GET | `/v1/status` | Palace overview (rooms, loci, chunks, TFC state) |
| POST | `/v1/search` | Hybrid search (`query`, `top_k`) |
| POST | `/v1/store` | Store memory chunk (`content`, `room`, `locus`) |
| POST | `/v1/assemble` | Retrieve + assemble context in one call |
| POST | `/v1/turn` | Store conversation turn with implicit feedback |
| POST | `/v1/feedback` | Log explicit feedback on a retrieved chunk |
| GET | `/v1/dashboard-data` | Dashboard data as JSON for frontend |

### Authentication

When `LUMMENNA_API_KEY` is set, all endpoints require `X-API-Key` header. Comparison uses constant-time string comparison.

---

## 8. CLI Reference

```bash
lummenna init [--device generic|rpi5|jetson-orin|orange-pi] [--download-model]
lummenna serve [--host 0.0.0.0] [--port 8848]
lummenna status

# Memory operations
lummenna memory store "content text" --room decisions [--locus topic]
lummenna memory retrieve "search query" [--top-k 5]

# Palace operations
lummenna palace rooms
lummenna palace stats [--room name]

# Compliance
lummenna compliance audit [--n 10]

# Daemon (background scheduler)
lummenna daemon start
lummenna daemon status
lummenna daemon run-once

# P2P sharing (requires LUMMENNA_SOVEREIGN=false)
lummenna p2p share --room name [--ttl 24]

# Model management
lummenna model list
lummenna model download bge-small-en-v1.5
lummenna model export bge-small-en-v1.5
```

---

## 9. Integrations

### MCP Server

```bash
python -m lummenna.integrations.mcp_server
```

Exposes 7 tools via stdio: `lummenna_search`, `lummenna_store`, `lummenna_assemble`, `lummenna_turn`, `lummenna_feedback`, `lummenna_status`, `lummenna_dashboard`.

### LangChain

```python
from lummenna.integrations.langchain import LummennaChatMemory
# Requires: pip install "lummenna[langchain]"
```

### LangGraph

```python
from lummenna.integrations.langgraph import LummennaCheckpointSaver
# Requires: pip install "lummenna[langgraph]"
```

---

## 10. Benchmarks

Eleven benchmark suites in `benchmarks/`. All run from the repo root and refuse to run with mock embeddings (stress allows an explicit, artifact-labeled `--mock-embedder` for storage-only runs).

| Suite | Status | Command |
|---|---|---|
| Retrieval | Run (synthetic corpus; 2 real embedders) | `python -m benchmarks.retrieval.run` |
| E2E quality | Run (real MiniLM embedder; self-graded persistence smoke test) | `python -m benchmarks.e2e.run` |
| Navigation | Run (~1.08× mean-latency speedup, ratio of means) | `python -m benchmarks.navigation.run` |
| Ablation | Run (latency only — accuracy identical across configs; corpus too easy) | `python -m benchmarks.ablation.run` |
| Forgetting | Run (results available) | `python -m benchmarks.forgetting.run` |
| Performance | Harness ready; no results yet | `python -m benchmarks.perf.run` |
| BEIR subset | Run (500-doc/20-query subset results available) | `python -m benchmarks.beir.run` |
| Optical degradation | Run (results available) | `python -m benchmarks.optical.run` |
| TFC sensitivity | Run (results available) | `python -m benchmarks.tfc.run` |
| Stress | Harness ready; no results yet | `python -m benchmarks.stress.run` |
| Cross-system (vs Chroma/FAISS) | Harness ready; no results yet | `python -m benchmarks.cross_system.run` |
| All suites | Wrapper (all 11) | `python -m benchmarks.run_all` |

Benchmarks use synthetic corpora unless real datasets (MS MARCO, BEIR) are explicitly loaded. Install benchmark dependencies with `pip install "lummenna[benchmarks]"`.

---

## 11. Security Model

- **Sovereign by default:** All external network calls blocked unless `LUMMENNA_SOVEREIGN=false`
- **Local embeddings:** ONNX Runtime, no external embedding API calls
- **PII scanning:** Regex-based at storage time with block/redact/hash strategies
- **Audit trail:** All operations logged to SQLite `audit_log` table
- **File permissions:** `~/.lummenna` enforced to 700 (dirs) / 600 (files) on startup
- **API auth:** Optional `X-API-Key` header with constant-time comparison (off until `LUMMENNA_API_KEY` is set)
- **Encryption-at-rest (opt-in):** SQLCipher full-DB or Fernet field-level via `LUMMENNA_DATABASE_ENCRYPTION_MODE`; default `none` — use OS-level disk encryption if not enabled

Report vulnerabilities via [GitHub Security Advisories](https://github.com/QuantmindSSI/lummenna/security/advisories/new).

---

## 12. Known Limitations

1. **Encryption-at-rest is opt-in** — SQLCipher and Fernet modes are implemented and tested but disabled by default (`LUMMENNA_DATABASE_ENCRYPTION_MODE=none`). In Fernet mode, the FTS5 lexical index still contains plaintext tokens (see SECURITY.md).
2. **P2P is LAN-only** — Beam transmits over TCP with AES-256-GCM encryption; not hardened for WAN (replay-nonce cache is in-memory only)
3. **Benchmark results are subset-scale** — synthetic retrieval corpus is deliberately easy; BEIR results cover 500-doc/20-query subsets; perf and stress now have committed results (x86_64 VM), while cross-system has no committed results. Full-corpus BEIR leaderboard submission remains pending.
4. **`vector_mean` / `last_entry_at` / `locus_count` unpopulated** — these schema columns are reserved but no code path writes them yet (loci have no maintained centroid). `learn_weights_from_feedback` (V(m) weight learning) and `backup_database` ARE wired — invoked from `conversation.py`/`langchain.py` and the sleep scheduler respectively.
5. **Dual scheduler risk** — running both `lummenna serve` and `lummenna daemon start` creates duplicate schedulers

---

## 13. Project Structure

```
lummenna/
├── config.py              # Pydantic-settings configuration
├── controller.py          # Twin-Force state controller
├── search.py              # Search pipeline orchestration
├── fusion.py              # RRF fusion + FRQAD/cosine reranking
├── conversation.py        # Context assembly + turn tracking
├── repair.py              # Self-healing retrieval
├── intent.py              # Intent classifier (keyword + logistic regression)
├── sleep.py               # Background consolidation scheduler
├── audit.py               # SQLite audit event logging
├── api/                   # FastAPI server + dashboard
├── cli/                   # Typer CLI
├── data/                  # Schema, migrations, backup
├── brand/                 # Error hierarchy
├── compliance/            # Safety forgetting, PII audit
├── force/
│   ├── mnemonic/          # Store, retrieval, decay, interference, eviction, provenance
│   └── contextual/        # Embedding, token budget, assembly
├── integrations/          # LangChain, LangGraph, MCP server
├── p2p/                   # Beam P2P sharing protocol
├── sovereign/             # FRQAD, optical quantization, local LLM
tests/                     # 41 test files, 294 tests
benchmarks/                # 11 benchmark suites (including BEIR, optical, TFC sensitivity, stress, cross-system)
```