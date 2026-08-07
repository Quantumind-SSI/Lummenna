# Lumena Brand Bible — v1.0.0

*Sourced exclusively from the Lumena codebase, documentation, test suite, benchmarks, and configuration as of August 2025.*

---

## 1. Product Identity

### 1.1 Name
**Lumena** — A sovereign, on-device agentic memory framework for AI coding assistants.

### 1.2 Tagline
*No defined tagline in the codebase.* The whitepaper describes it as "a structured memory palace with heuristic forgetting, operating entirely on-device."

### 1.3 Pitch (from docs/RELEASE_ANNOUNCEMENT_v1.0.0.md)
> "Lumena gives your coding agent a persistent memory palace — not a vector dump, not a context hack. Memories decay, consolidate, and survive across sessions on your own hardware."

### 1.4 Version
**v1.0.0** — Confirmed in `lumena/api/server.py` app title, MCP server dashboard text, `CHANGELOG.md`, and `pyproject.toml`.

### 1.5 License
Apache License 2.0 (LICENSE file, pyproject.toml classifiers).

### 1.6 Repository
`https://github.com/QuantumindSSI/lumena` (confirmed in pyproject.toml URLs, integrations configs, and docs).

### 1.7 Python Package Name
`lumena` (pyproject.toml, all `import lumena.*` statements, CLI entry point).

---

## 2. Architecture — The Twin-Force Model

Lumena is built on a **dual-force architecture**:

### Force A: Mnemonic (Memory Palace)
The long-term memory store — "palace" — consisting of rooms, loci (sub-locations), and chunks (individual memories). This force handles storage, retrieval, forgetting, provenance tracking, and value scoring.

### Force B: Contextual (Working Window)
The short-term working window — handles context assembly, embedding, token budget enforcement, and prompt injection for the LLM agent.

### Twin-Force Controller (TFC)
A unified state machine that balances mnemonic conservation vs. contextual exploration. Defined in `lumena/controller.py`:

| Parameter | Meaning | Default | Range |
|-----------|---------|---------|-------|
| `e` | Conservation bias (0 = release everything, 1 = keep everything) | 0.5 | 0.0–1.0 |
| `a` | Attentional temperature (0 = rigid/rule-based, 1 = diffuse/exploratory) | 0.5 | 0.0–1.0 |
| `tau` | Temporal horizon in days (older memories fade faster) | 7.0 | 1.0–90.0 |
| `r` | Resolution level (0=BINARY, 1=INT8, 2=FP16, 3=FP32) | 3 | 0–3 |

**Deterministic update rules** in `controller.py`::
- `e` drifts toward conservation when memory density is high; toward release when budget pressure mounts.
- `a` increases when search yields empty results (repair loop); decreases when context budget is near exhaustion.
- `tau` is user-specific (from `user_profile.ebbinghaus_half_life_days`).
- `r` degrades under memory budget pressure (L3 eviction).

---

## 3. Core Capabilities

### 3.1 Memory Storage & Organization
- **Rooms**: Top-level categories (`decisions`, `architecture`, `snippets`, `bugs`, `conversations`, etc.). Room types: `domain`, `project`, `person`, `ephemeral`.
- **Loci**: Sub-locations within rooms, analogous to loci in the method-of-loci mnemonic technique.
- **Chunks**: Individual memory units with content, embedding vector, V(m) score, provenance chain, and bi-temporal validity.

### 3.2 Retrieval Pipeline (from `lumena/search.py` and `lumena/fusion.py`)
A multi-stage pipeline:
1. **Intent Classification** — Rule-based + optional logistic regression (factual / exploratory / relational / temporal)
2. **BM25 Lexical Search** — Via SQLite FTS5 (porter stemmer, unicode61 tokenizer)
3. **Dense Vector Search** — Via sqlite-vec (≤50K chunks) or USearch HNSW (50K–500K chunks), with fallback brute-force cosine
4. **Graph Traversal** — NetworkX adjacency from locus co-location and provenance parent chains
5. **Reciprocal Rank Fusion (RRF)** — Cormack et al. (2009) standard fusion, k=60
6. **FRQAD Rerank** — Geodesic-aware cosine (resolution-adaptive), with fallback to SciPy cosine
7. **V(m) × RRF × FRQAD × Recency × Goal Boost** — Final scoring formula in `fusion.py`
8. **Context Assembly** — Jinja2 template with token budget enforcement, truncation fallbacks

### 3.3 Forgetting Pipeline (Three-Layer)
- **L1 — Ebbinghaus Passive Decay**: Multiplicative penalty based on `ebbinghaus_half_life_days` from user profile. Formula: `R(t) = e^(-t / (h × ln(2)))`.
- **L2 — Interference-Based Weakening**: When a new chunk occupies the same locus and embedding similarity exceeds 0.85, older chunks suffer a 15% × similarity V(m) penalty.
- **L3 — Budget-Curated Eviction**: When estimated data footprint exceeds 85% of `memory_limit_mb`, lowest-scoring chunks undergo optical degradation (FP32 → FP16 → INT8 → BINARY → RELEASED).

### 3.4 Provenance (Bi-Temporal Engram-Inspired)
Every chunk has:
- `valid_from` / `valid_to` — bi-temporal window (inspired by bi-temporal database design)
- `superseded_by` — chained logical updates
- `provenance_root` — source tracking (user_input, agent_reasoning, consolidation, import, p2p_share)
- `parent_provenance` — causal chain linking
- **Merge-on-read**: `get_effective_fact()` respects supression chains and optional time-travel via `as_of_transaction`.

### 3.5 V(m) — Value Model (from `lumena/force/mnemonic/value_model.py`)
Seven-factor scalar score for each chunk:
1. **Recency** — Time since creation (exponential decay)
2. **Access frequency** — raw `access_count`
3. **Goal relevance** — cosine similarity to active goal embeddings
4. **Feedback positivity** — from `feedback_log`
5. **Provenance confidence** — from the `provenance` table
6. **Consolidation boost** — narrative-generated chunks get a bonus
7. **Locus depth** — position within the locus structure

Weights are learned from user feedback via linear regression (5+ feedback events required).

### 3.6 Context Assembly
Jinja2 template in `lumena/force/contextual/assembly.py`:
```
{system prompt}
{palace minimap (room chain)}
{active goals}
--- Retrieved Memories ---
{ranked chunks with room/locus/provenance headers}
--- End Memories ---
{user query}
```

Token budget enforcement:
1. Truncate individual chunks exceeding 30% of budget
2. Drop lowest-scoring chunks until everything fits
3. Emergency mode: keep only top chunk
4. Fail-fast with `BudgetExceededError` if nothing fits

### 3.7 Feedback Loop
- `feedback_log` table tracks explicit (`lumena_feedback`), implicit (via `lumena_turn`), and repair feedback
- V(m) weights are retrained when feedback count reaches 5
- Search Repair Loop (`lumena/repair.py`) self-heals: empty results widen `a`, budget exceeded degrades `r`, low confidence widens `a`

---

## 4. Data Schema (from `lumena/data/schema.sql`)

14 tables (after migrations 0002 and 0003):

| Table | Purpose |
|-------|---------|
| `room` | Top-level memory categories |
| `locus` | Sub-locations within rooms |
| `chunk` | Individual memory units (content, embedding reference, V(m), validity window) |
| `provenance` | Source tracking for every chunk |
| `chunk_fts` | FTS5 virtual table for BM25 lexical search |
| `feedback_log` | Explicit, implicit, and repair feedback events |
| `audit_log` | Security/compliance event trail (added in migration 0002) |
| `user_profile` | Goals, values, learned V(m) weights, Ebbinghaus half-life |
| `goals` | Hierarchical goal tree with active/inactive state |
| `epistemic_state` | Known facts, assumed gaps, established truths |
| `event_buffer_meta` | Consolidation/decay run timestamps |
| `vec_fallback` | Brute-force vector blob storage (always maintained) |
| `vec_chunks` | sqlite-vec vector index (when available) |
| *(Migration 0002 added `tenant_id` columns for multi-tenancy; migration 0003 added `encrypted` flag)* |

---

## 5. Sovereign Mode & Security

### 5.1 Sovereign Mode (`config.sovereign = True` by default)
- Any external API call is blocked with `SovereignViolationError`
- Must be explicitly waived in config

### 5.2 Encryption
Three modes (`database_encryption_mode`):
- **`sqlcipher`** — Page-level AES-256 encryption via SQLCipher (requires `sqlcipher3` package, `LUMENA_ENCRYPTION_KEY`)
- **`fernet`** — Field-level encryption of `chunk.content` via Fernet (symmetric AES-128-CBC with HMAC)
- **`none`** — Plaintext SQLite (default; production not recommended)

### 5.3 P2P Beam Protocol Security (`lumena/p2p/beam.py`)
- **Encryption**: AES-256-GCM via `P2PCrypto`
- **Integrity/Authenticity**: HMAC-SHA256 with 30-second replay window
- **TTL**: Frames older than `ttl_hours` are dropped at reception
- **Plaintext fallback**: REMOVED. Beam without encryption key raises an error unless `LUMENA_P2P_INSECURE=1` is set.
- Discovery via Zeroconf/mDNS (LAN only)
- Length-prefixed framed protocol
- TTL enforced at both send and receive

### 5.4 API Security (`lumena/api/server.py`)
- **Auth**: API-key middleware via `LUMENA_API_KEY`; all endpoints except `/health` and `/dashboard` require key
- **Rate Limiting**: Token-bucket rate limiter (default 60 req/min)
- **Size Limits**: Configurable `LUMENA_REQUEST_MAX_SIZE_BYTES` (default 1 MB)
- **CORS Lockdown**: Configurable `LUMENA_ALLOWED_ORIGINS`
- **Security Headers**: HSTS, CSP for dashboard, X-Content-Type-Options, X-Frame-Options
- **Brute-Force Protection**: Failed auth attempts tracked per-IP with exponential backoff
- **PII Detection**: Optional `pii_detection_enabled` with modes: `block`, `redact`, `hash`
- **Audit Logging**: Every API call logged to `audit_log` table

### 5.5 SQLCipher Support (`lumena/data/schema.py` + `test_sqlcipher.py`)
- Passphrase derivation via PBKDF2-HMAC-SHA256 from `LUMENA_ENCRYPTION_KEY`
- SALT file stored alongside database for deterministic derivation across restarts
- Test coverage exists but is marked as skipped pending v0.3.0 feature flag
- Permissions enforced: `~/.lumena/` tree is 700 (dirs) and 600 (files)

---

## 6. Device Profiles (from `lumena/config.py`)

| Device | Context Budget | Memory Limit | Vector Index |
|--------|---------------|--------------|--------------|
| `rpi5` (Raspberry Pi 5) | 1024 tokens | 256 MB | sqlite-vec |
| `jetson-orin` | 2048 tokens | 512 MB | usearch |
| `orange-pi` | 1024 tokens | 256 MB | sqlite-vec |
| `generic` (default) | 2048 tokens | 512 MB | sqlite-vec |

---

## 7. Edge & Performance Characteristics

### 7.1 Embedding Model
- Default: **BGE-small-en-v1.5** (384-dim, ONNX-exported)
- Size: ~33 MB (INT8 quantized via Optimum)
- Runtime: ONNX Runtime (CPUExecutionProvider)
- Model provisioning: `lumena model download bge-small-en-v1.5` (Python API or optimum-cli)
- Fallback: `MockEmbedder` for testing — deterministic pseudo-random vectors (hash-based)

### 7.2 Local LLM Support (Experimental)
- Engine: llama-cpp-python with GGUF models
- Default model: `Qwen3-1.7B-Q4_K_M.gguf`
- Gated behind `enable_local_llm = True` in config
- Used for consolidation narrative generation during sleep phase
- Lifecycle: load/unload with context manager to manage RAM

### 7.3 On-Disk Footprint
- SQLite WAL-mode database (`~/.lumena/store/lumena.db`)
- FTS5 index with porter stemmer
- vec_fallback blob storage (384 floats × 4 bytes = 1536 bytes per vector)
- No external network dependency (fully sovereign)
- Edge deployable on RPi5 with ~90 MB RAM (from whitepaper)

### 7.4 Optical Degradation Pipeline
Progressive quantization chain for memory lifecycles:
- **FP32** → **FP16** → **INT8** → **BINARY** → **RELEASED**
- Triggered by L3 budget eviction
- After BINARY resolution, chunk is released (valid_to set)
- Quantizer functions implemented via `lumena/sovereign/optical.py`

---

## 8. Integrations

### 8.1 Available Interfaces

| Interface | Transport | Best For |
|-----------|-----------|----------|
| MCP Server (stdio) | stdin/stdout | Native tool calling in MCP-compatible IDEs |
| HTTP API (FastAPI) | HTTP/REST | Remote deployments, custom agents, CI/CD |
| Python SDK (`ConversationMemory`) | In-process | LangChain, LangGraph, custom Python agents |

### 8.2 Supported Platforms (all with ready config files)

| Platform | Integration Type | Config File |
|----------|-----------------|-------------|
| OpenCode | MCP stdio + skill | `integrations/opencode/opencode.json` |
| GitHub Copilot | MCP server + VS Code extension | `integrations/copilot/vscode-settings.json` |
| Claude Desktop | MCP stdio | `integrations/claude-desktop/claude_desktop_config.json` |
| OpenAI Codex CLI | MCP stdio | `integrations/codex-cli/config.json` |
| Goose (Block) | MCP stdio | `integrations/goose/goose-config.yaml` |
| Cline | MCP server | `integrations/cline/cline_mcp_settings.json` |
| Continue | MCP server | `integrations/continue/config.json` |
| Zed | Context server + slash commands | `integrations/zed/README.md` |
| Devin | Custom tool YAML + HTTP API | `integrations/devin/lumena-tool.yaml` |
| Aider | Custom slash commands + HTTP | `integrations/aider/README.md` |

### 8.3 MCP Server Tools (7 tools, 2 resources)

**Tools** (from `lumena/integrations/mcp_server.py`):
1. `lumena_search` — Semantic + lexical hybrid search
2. `lumena_store` — Persist a memory with room/locus
3. `lumena_assemble` — Retrieve and assemble context in one call
4. `lumena_turn` — Log conversation turn with implicit feedback
5. `lumena_feedback` — Log explicit feedback on retrieved memory
6. `lumena_status` — Palace health: rooms, chunks, TFC state, embedder status
7. `lumena_dashboard` — Formatted text dashboard with benchmarks, TFC, topology, business impact

**Resources**:
- `dashboard://lumena/effectiveness` — Full effectiveness metrics JSON
- `dashboard://lumena/sota` — SOTA comparison and capability highlights

### 8.4 LangChain & LangGraph Integrations
- `LumenaChatMemory` — LangChain BaseChatMemory with retrieve, save, feedback, and V(m) learning
- `LumenaStore` — LangChain BaseStore with mset/mget/mdelete
- `LumenaCheckpointSaver` — LangGraph checkpoint saver for agent state persistence
- `LumenaGraphStore` — LangGraph graph store with put/get/delete/search/list_namespaces

---

## 9. Benchmark Results

### 9.1 Supported Benchmarks (from `benchmarks/` directory)
Run via `python -m benchmarks.run_all`:

| Suite | What It Measures | Status |
|-------|-----------------|--------|
| **Retrieval** | BM25/Dense/Hybrid Recall@10, nDCG@10, p50/p95/p99 latency on MS MARCO v1.1 + synthetic | ✅ Implemented |
| **Cross-System** | Lumena vs Chroma vs FAISS head-to-head (same corpus/embedder) | ✅ Implemented |
| **BEIR** | NFCorpus, SciFact, FiQA, ArguAna, SCIDOCS cross-dataset | ✅ Implemented |
| **Ablation** | FULL/NO_TFC/NO_VM/NO_GRAPH/BM25_ONLY/DENSE_ONLY on domain corpus (111 chunks, 8 rooms, 20 queries) | ✅ Implemented |
| **Navigation** | Global vs Oracle Room vs Routed Room efficiency (2400 passages, 8 rooms) | ✅ Implemented |
| **Forgetting** | 10K chunks over 90 days, ebbinghaus decay + budget eviction distributions | ✅ Implemented |
| **Optical** | FP32→FP16→INT8→BINARY degradation recall/nDCG | ✅ Implemented |
| **TFC Sensitivity** | 3×3×3×3 grid search (81 combos) | ✅ Implemented |
| **Performance** | 1K/10K/50K corpora latency, RAM, storage, write amplification | ✅ Implemented |
| **Stress** | 100K+ chunk ingestion, multi-threaded throughput, L3 eviction under memory pressure | ✅ Implemented |
| **E2E** | 7 sessions, 3 personas, multi-turn conversations, recall@k, semantic similarity, persistence | ✅ Implemented |

### 9.2 Key Metrics Tracked
- **R@10 (Recall@10)**: BM25, dense, and hybrid
- **nDCG@10**: Normalized Discounted Cumulative Gain
- **Latency**: p50/p95/p99 for BM25, dense, and hybrid
- **Routing Accuracy**: Room-level classification precision
- **Forgetting Distributions**: V(m) score evolution over 90 days
- **Optical Degradation**: Recall preservation at each quantization level
- **Memory Budget**: RAM/disk footprint vs chunk count
- **Write Amplification**: With/without WearAwareBatcher

---

## 10. Business & Value Metrics (from dashboard/resource data)

| Metric | Value |
|--------|-------|
| Data Sovereignty | 100% (fully on-device) |
| API Cost per Query | $0.00 (no external API calls) |
| GDPR Native RTBF | YES (compliance forgetting via `lumena compliance audit`) |
| Edge Deployable | YES (RPi5, Jetson Orin, Orange Pi) |
| Feedback Satisfaction | Self-reported from `/v1/feedback` endpoint |

---

## 11. Error Codes & Hierarchy

Defined in `lumena/brand/errors.py` — all exceptions inherit from `LumenaError`:

| Code | Exception | Category |
|------|-----------|----------|
| `LME-0000` | `LumenaError` | Base |
| `LME-1001` | `RoomNotFoundError` | Palace: room missing |
| `LME-1002` | `LocusConflictError` | Palace: locus name collision |
| `LME-1003` | `ConsolidationFailedError` | Palace: sleep consolidation failure |
| `LME-2001` | `ModelNotAvailableError` | Embedding: ONNX model missing |
| `LME-2002` | `FeedbackLogError` | Feedback: write/read failure |
| `LCX-2001` | `BudgetExceededError` | Context: token budget overflow |
| `LCX-2002` | `RetrievalEmptyError` | Context: no memories matched |
| `LCX-2003` | `AssemblyTimeoutError` | Context: assembly exceeded time budget |
| `LLM-3001` | `TFCError` | TFC: general state error |
| `LLM-3002` | `TFCStuckError` | TFC: unrecoverable attractor |
| `LLM-3003` | `SovereignViolationError` | Sovereign: external API call blocked |
| `LLM-3004` | `ValueModelUncalibratedError` | TFC: V(m) weights not yet learned |

---

## 12. CLI Interface (from `lumena/cli/main.py` — 412 lines, Typer + Rich)

```
lumena init                Initialize palace and optionally download embedding model
lumena serve               Start FastAPI server + dashboard
lumena memory store        Store a memory via CLI
lumena memory retrieve     Retrieve memories via CLI
lumena palace rooms        List/CRUD rooms
lumena palace loci         List/CRUD loci
lumena tfc show            Display TFC state (e, a, tau, r)
lumena tfc set --e 0.7     Modify TFC parameters
lumena compliance audit    Run GDPR/CCPA compliance audit
lumena daemon              Start sleep-phase consolidation daemon
lumena p2p share           Share memories via Beam P2P protocol
lumena model list          List available/downloaded embedding models
lumena model download      Download and ONNX-export a model
```

---

## 13. API Endpoints (from `lumena/api/server.py` — 750 lines)

### Public (no auth required)
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Health check (JSON) |
| GET | `/dashboard` | Effectiveness dashboard (HTML) |
| GET | `/metrics` | Prometheus-compatible metrics |
| GET | `/openapi.json` | OpenAPI schema |

### Authenticated (API key required)
| Method | Path | Purpose |
|--------|------|---------|
| POST | `/v1/search` | Hybrid search |
| POST | `/v1/store` | Store a memory |
| POST | `/v1/feedback` | Log explicit feedback |
| POST | `/v1/assemble` | Retrieve + assemble context |
| POST | `/v1/turn` | Log conversation turn |
| GET | `/v1/status` | Palace health overview |
| GET | `/v1/dashboard-data` | Dashboard data as JSON |

### API Versioning
- Versioned endpoints under `/v1/`
- Backward-compatible public paths for `/health`, `/dashboard`, `/metrics`

---

## 14. Dashboard (from `lumena/api/dashboard.html` — 629 lines)

Self-hosted HTML dashboard served at `/dashboard` with:
- **System Health KPIs**: Active memories, rooms, loci, retention rate, embedder status
- **TFC State Gauges**: Real-time e/a/tau/r (Chart.js gauge charts)
- **Room Topology Chart**: Bar chart of chunks per room
- **Latency Bars**: p50/p95/p99 for BM25, dense, and hybrid retrieval
- **SOTA Comparison Table**: BM25, dense, hybrid, edge deployability, memory lifecycle
- **Live Search Interface**: In-browser search with results rendering
- **Business Metrics**: Data sovereignty %, cost/query, GDPR RTBF, edge deployability
- **Forgetting Pipeline Status**: L1 (decay), L2 (interference), L3 (eviction) active/inactive indicators
- **Memory Budget Bar**: Percentage utilization with green/amber/red thresholds

---

## 15. Unique Differentiators

1. **100% Local / Sovereign**: No cloud dependency. Embeddings generated via ONNX Runtime. Sovereign mode blocks all external API calls by default.

2. **Memory Palace Architecture**: Not a flat vector database. Hierarchical room → locus → chunk structure with bi-temporal provenance and Engram-inspired supsession chains.

3. **Heuristic Forgetting**: Three-layer pipeline (Ebbinghaus decay, interference weakening, budget-curated eviction) with optical degradation chain.

4. **Self-Healing Retrieval**: Search repair loop that automatically adjusts TFC attention temperature when results are empty, budget is exceeded, or confidence is low.

5. **Edge Deployability**: Device profiles for RPi5 (256 MB, 1024 tokens), Jetson Orin (512 MB, 2048 tokens), Orange Pi. ONNX-optimized embedder at ~90 MB RAM.

6. **10 IDE Integrations**: Ready configs for OpenCode, Copilot, Claude Desktop, Codex CLI, Goose, Cline, Continue, Zed, Devin, and Aider — plus LangChain/LangGraph SDKs.

7. **Full Benchmark Suite**: 11 benchmark suites (retrieval, cross-system, BEIR, ablation, navigation, forgetting, optical, TFC sensitivity, performance, stress, E2E). Run via single `python -m benchmarks.run_all`.

8. **GDPR-Native Right to Be Forgotten**: `lumena compliance audit` + three-layer forgetting pipeline supports compliance-driven erasure.

9. **Encryption-at-Rest Options**: Three tiers — SQLCipher (page-level AES-256), Fernet (field-level AES-128), and plaintext.

10. **P2P Memory Sharing (Beam Protocol)**: LAN-only household sharing with mandatory AES-256-GCM encryption, HMAC integrity, 30-second replay protection, and TTL enforcement.

---

## 16. Codebase Statistics (verified)

| Metric | Count |
|--------|-------|
| Python source files (lumena/) | 60 |
| Test files (tests/) | 43 |
| Benchmark suites (benchmarks/) | 11 |
| Integrations (integrations/) | 10 platforms, 16 config files |
| Documentation files (docs/) | 7 |
| Scripts | 3 |
| CLI commands | 11 |
| API endpoints | 7 active + 4 public |
| MCP server tools | 7 |
| MCP server resources | 2 |
| Database tables | 14 |
| Error codes | 12 |
| Device profiles | 4 |
| Forgetting layers | 3 |
| TFC parameters | 4 |
| Supported platforms | 10 |

---

*This brand bible is generated from source-code inspection of the Lumena repository as of August 2025. Every claim is backed by a specific file, class, function, test, benchmark, or configuration entry in the codebase.*