# Lumen: A Production-Oriented Agentic Memory Framework with Structured Topology, Twin-Force Control, and Native Integrations

**QuantumindSSI Research — Engineering Report v0.2.0 (Beta)**

**Pre-production release. Core architecture is stable; BEIR/MTEB leaderboard submission and large-scale human evaluation remain as future work.**

---

## Abstract

We present Lumen, an open-source agentic memory system that organizes agent memories in a structured *memory palace* topology (rooms, loci, corridors) governed by a heuristic *Twin-Force Controller* (TFC) that balances mnemonic conservation against contextual attention. Lumen provides native third-party integrations, a hardened API surface, and validated benchmark suites. Key subsystems include: a LangGraph checkpoint saver and graph store for sovereign agent-state persistence; a Model Context Protocol (MCP) server for OpenCode and Claude Desktop; a Beam P2P sharing protocol for household-local memory exchange; a wear-aware batch writer for SD/eMMC endurance; first-party benchmark suites (retrieval, forgetting, performance, palace navigation efficiency, end-to-end memory quality, component ablation, BEIR subset, optical degradation, and TFC sensitivity); and a hardened FastAPI server with authentication, rate limiting, and CORS. The system operates on-device via ONNX Runtime and SQLite with a ~90 MB RAM footprint, a 30–50% smaller storage footprint than comparable vector stores, and no metered external API cost. On small-scale pilots (28 queries, 41 feedback ratings), we report 1.32–1.50× latency speedup from room-constrained search versus flat global search with >99.8% recall retention, 100% R@10 and 78.6% R@5 in multi-turn E2E testing (mock embedder, committed results), and 95.1% implicit feedback satisfaction. These numbers are directionally promising but reflect limited sample sizes and a low-cardinality benchmark structure. We explicitly identify open limitations, most critically that the default forgetting pipeline performs unconditional decay (all memories fade without a re-access reinforcement mechanism) — the primary gating item for any production-ready claim — along with the absence of full BEIR/MTEB leaderboard validation.

---

## 1. Introduction

### 1.1 The Agentic Memory Problem

Large language model (LLM) agents require persistent memory across conversation turns, yet their effective working memory is bounded by context windows that range from 4K to 2M tokens (Liu et al., 2024). Simple append-only conversation buffers quickly saturate this window with irrelevant history. Retrieval-augmented generation (RAG) extends capacity by fetching external documents, but existing RAG and agentic memory implementations treat memory as unstructured collections — flat vector databases without lifecycle management, spatial organization, or learned value ranking.

### 1.2 Three Architectural Patterns

The agentic memory landscape is dominated by three patterns that share critical limitations:

1. **Pure vector stores** (Chroma, FAISS, Qdrant, Weaviate, Milvus) provide fast approximate nearest-neighbor search but no memory lifecycle, forgetting, or topology.
2. **Cloud-hosted agent memory services** (MemGPT/Letta, Zep, Mem0, LangMem) manage conversation history and entity extraction but require persistent cloud connections.
3. **RAG orchestrators** (LlamaIndex, LangChain) coordinate retrieval over external vector stores but rely on external infrastructure.

Common to all three: the absence of a *forgetting mechanism*, the reliance on *flat retrieval* without spatial organization, and (in cloud services) *external API dependency* that violates data sovereignty.

### 1.3 Capabilities Overview

Lumen is a beta framework with the following concrete capabilities:

- **Native LangGraph integration** (`lumen.integrations.langgraph`): a `LumenCheckpointSaver` implementing LangGraph's `BaseCheckpointSaver` protocol, plus a `LumenGraphStore` for cross-thread long-term memory — enabling sovereign graph-agent state persistence without external checkpoints.
- **Model Context Protocol (MCP) server**: exposes search, store, assemble, turn, feedback, status, and dashboard operations to OpenCode, Claude Desktop, and GitHub Copilot via stdio transport.
- **Beam P2P sharing protocol**: household-local memory sharing with mandatory AES-256-GCM encryption, HMAC-SHA256 integrity signing, 30-second replay protection, and service discovery via Zeroconf. Plaintext transport requires explicit `LUMEN_P2P_INSECURE=1` (testing only).
- **Benchmark suites**: retrieval (R@k, nDCG, MAP, MRR), forgetting (90-day survival curves, interference precision), performance (latency p50/p95/p99, footprint), palace navigation efficiency (room-constrained speedup vs. global), end-to-end memory quality (multi-turn conversational recall with 7 personas, 28 queries), component ablation, BEIR subset retrieval, optical degradation, and TFC sensitivity.
- **Domain corpus and seeding**: a hand-crafted 111-chunk, 8-room knowledge base used for both palace seeding and benchmarking.
- **Hardened API surface**: FastAPI server with API-key authentication, slowapi rate limiting, CORS middleware, request-size limits, request-ID propagation, and a global unhandled-exception handler.
- **Wear-aware batch writer**: defers and coalesces SQLite writes to reduce SD/eMMC flash wear on embedded devices (Raspberry Pi, Jetson, Orange Pi).
- **Effectiveness dashboard**: self-hosted HTML dashboard with real-time palace topology, retrieval metrics, Twin-Force Controller state, forgetting pipeline health, and SOTA comparison — served at `/dashboard` with no external network or CDN dependencies (all assets bundled).
- **Test coverage**: 41 test files containing 273 tests (9 skipped), ~74% line coverage, covering core storage, retrieval, forgetting, conversation, API server, LangChain/LangGraph/MCP integrations, P2P Beam, optical degradation, wear batching, encryption, and user scenarios.

### 1.4 Design Principles

Lumen is governed by four design principles:

1. **Sovereignty first**: all computation — embedding, storage, retrieval, consolidation — runs on-device via ONNX Runtime and SQLite. `LUMEN_SOVEREIGN=true` blocks every external API call.
2. **Structured by default**: memories are not flat vectors; they live in rooms, loci, and corridors with provenance chains and bi-temporal validity markers.
3. **Lifecycle-aware**: memories are born, ranked, decay, interfere, degrade, and are released — not merely inserted and deleted.
4. **Integrable, not isolated**: native adapters for LangChain, LangGraph, MCP, and FastAPI REST allow Lumen to plug into existing agent stacks without architectural lock-in.

---

## 2. System Architecture

### 2.1 Memory Palace Topology

Lumen structures memory as a graph with three entity types spanning 10 SQLite tables (WAL mode):

- **Rooms** (*n* configurable, typically 8–50): top-level categories with a domain-type classification (`domain`, `project`, `person`, `ephemeral`).
- **Loci** (*m* per room, typically 4–20): sub-locations acting as semantic clusters, each maintaining a cached vector centroid.
- **Chunks** (*k* per locus): atomic memory units containing raw text, a 384-dimensional embedding vector (BGE-small or MiniLM), a V(m) heuristic score, bi-temporal validity markers (`valid_from`, `valid_to`), and a resolution quantifier (`FP32 → FP16 → INT8 → BINARY → RELEASED`).

Storage footprint is ~35 MB per 10,000 chunks in a single-file SQLite database.

### 2.2 Twin-Force Controller (TFC)

The TFC maintains four state variables balancing memory depth against contextual attention:

- **e ∈ [0, 1]**: mnemonic conservation bias (high = preserve memories, low = attend to new information)
- **a ∈ [0, 1]**: attentional temperature (high = exploratory retrieval, low = focused)
- **τ > 0**: temporal horizon in days (default 7)
- **r ∈ {0, ..., 5}**: resolution level controlling embedding quantization

State updates follow deterministic rules from interaction signals (novelty, repetition, context_pressure, satisfaction, goal_changed):

```
if novelty > 0.7:       e ← max(0.0, e - 0.1); a ← min(1.0, a + 0.1)
if repetition > 0.7:    e ← min(1.0, e + 0.1); a ← max(0.0, a - 0.1)
if context_pressure > 0.9:  r ← max(0, r - 1)
if satisfaction < -0.3: a ← min(1.0, a + 0.2)
if goal_changed:        τ ← 7.0; r ← 3
```

Thresholds (0.7, 0.9, −0.3) are hand-tuned; the learning pathway is still heuristic-only. The TFC's primary role in practice is to modulate retrieval scope and resolution under memory pressure.

### 2.3 Multi-Channel Retrieval

Retrieval proceeds through five stages:

**Stage 1 — Intent Classification.** Rule-based router classifies queries as factual, exploratory, relational, or temporal. Optional fastText classifier when training data is available.

**Stage 2 — Parallel Retrieval.** Three independent channels execute concurrently:
- **Lexical (BM25)**: SQLite FTS5 with Porter stemming.
- **Dense (Vector)**: sqlite-vec (≤50K vectors) or USearch HNSW (50K–500K) with cosine distance.
- **Graph (Structural)**: BFS traversal from top-3 dense-hit seeds, 2-hop expansion across provenance links and co-location edges.

**Stage 3 — Reciprocal Rank Fusion (RRF).** `k_RRF = 60`, graph hits weighted at 0.8×.

**Stage 4 — Multi-Factor Reranking.**
```
s(chunk) = RRF(chunk) · (V_m(chunk) + 0.1) · (sim(chunk) + 0.1) · recency(chunk) · goal_bonus
```
where `sim` uses FRQAD geodesic distance with resolution-dependent noise (σ²: FP32=0.0, FP16=10⁻⁴, INT8=0.02, BINARY=0.3), `recency = exp(−age_hours/168)`, and `goal_bonus = 1.2` if active goal keywords match.

**Stage 5 — Repair Loop.** If results are empty or all scores fall below 0.01, the pipeline re-executes with relaxed constraints (increased *k*, lower resolution threshold) up to one repair attempt.

### 2.4 V(m) Value Model

Each chunk receives a scalar value V(m) ∈ [0, 1] from seven heuristic features (default weights): goal_relevance (0.20), value_alignment (0.15), self_relevance via first-person pronoun density (0.15), task_utility via action-verb matching (0.15), emotional_intensity via lexicographic sentiment polarity (0.15), reliability via source-type lookup (0.10), and usage_history (0.10). Weights are combined linearly and passed through a sigmoid. The Nelder-Mead learning pathway (minimizing `L = −(μ_pos − μ_neg)`) activates at ≥10 feedback samples.

The `/turn` API endpoint resolves real `room_name`, `locus_name`, `vm_score`, and `age_hours` from the database, making V(m) and provenance data observable to callers and downstream dashboards.

### 2.5 Forgetting Pipeline

Four layers of heuristic forgetting operate at different timescales:

**L1 — Exponential decay (daily).** `R(t) = exp(−t / (τ_h · 86400 · ln(2)))` with default `τ_h = 7` days. Chunks with V(m) < 0.05 are queued for release. A recency boost of `exp(−age_hours/24)` provides a floor for very recent memories.

At default `τ_h = 7`, all 10,000 synthetic chunks decay below threshold within 90 days. This behavior is mathematically correct but does not match the design intent of selective curation. A re-access reinforcement mechanism (a retrieval-time boost to V(m)) is planned to close this gap. The pipeline therefore performs unconditional decay at default settings.

**L2 — Interference weakening (on write).** When a new chunk enters a locus, existing residents with cosine similarity > 0.85 receive a V(m) penalty of `0.15 × similarity`. The interference checker reads embeddings from `vec_chunks` when `vec_fallback` is empty, ensuring interference checks function correctly in sqlite-vec-only deployments.

**L3 — Budget eviction (periodic).** When RSS exceeds 85% of the memory limit, low-scoring chunks are evicted through the degradation chain (`FP32 → FP16 → INT8 → BINARY → RELEASED`). The optical degradation path transitions properly through each precision stage before release.

**L4 — Compliance deletion (on write).** PII patterns trigger redaction and immediate release.

---

## 3. Integrations and Subsystems

### 3.1 LangGraph Integration (`lumen.integrations.langgraph`)

Lumen ships with two LangGraph-native adapters, installable via `pip install lumen[langgraph]`:

- **`LumenCheckpointSaver`**: implements LangGraph's `BaseCheckpointSaver` protocol. Each `thread_id` receives its own locus inside a dedicated `langgraph` room, providing natural isolation and thread-scoped retrieval. Checkpoints are stored as structured memories with provenance chains referencing parent checkpoints.
- **`LumenGraphStore`**: provides cross-thread long-term memory for LangGraph graphs, enabling agents to reference facts learned in one thread while executing another.

The integration includes 9 test cases verifying checkpoint round-trips, async iteration (`alist`), and state consistency across writes.

### 3.2 Model Context Protocol (MCP) Server

The MCP server (`lumen.integrations.mcp_server`) exposes Lumen operations to MCP-compatible hosts (OpenCode, Claude Desktop, GitHub Copilot) via stdio transport:

- `lumen_search` — hybrid semantic + lexical search
- `lumen_store` — store a memory chunk with room/locus/source-type
- `lumen_assemble` — retrieve and assemble context in one call
- `lumen_turn` — store a full conversation turn and log implicit feedback
- `lumen_feedback` — log explicit or implicit feedback for a retrieved chunk
- `lumen_status` — palace overview with room/chunk counts and TFC state
- `lumen_dashboard` — display real-time effectiveness dashboard with benchmarks

This enables agents running inside Claude Desktop or OpenCode to read from and write to the user's local Lumen palace without custom API clients.

### 3.3 Beam P2P Sharing Protocol (`lumen.p2p.beam`)

Beam is a household-local P2P memory sharing protocol with the following security model:

- **Scope**: LAN-only, discovered via Zeroconf (`_lumen-beam._tcp`).
- **Encryption**: AES-256-GCM (via `P2PCrypto`) mandatory by default. A `p2p_encryption_key` is required; plaintext transport is only available with explicit `LUMEN_P2P_INSECURE=1` for local testing.
- **Integrity**: HMAC-SHA256 signing with 30-second replay window.
- **Permission decay**: shared memories receive a time-to-live and degrade automatically.
- **Transport**: length-prefixed msgspec JSON over asyncio TCP streams.

Beam is not production-hardened for adversarial WAN environments; it is designed for trusted household or lab environments where multiple Lumen instances (e.g., a Raspberry Pi agent and a laptop agent) share a common memory substrate.

### 3.4 Wear-Aware Batch Writer (`lumen.sovereign.wear`)

Embedded deployments on Raspberry Pi, Orange Pi, and Jetson use SD/eMMC storage with finite write endurance. The `WearAwareBatcher` coalesces SQLite writes into deferred bulk flushes, achieving 2–15× speedup over naive per-row execution and reducing flash wear. This is particularly important for the consolidation path, which performs frequent small writes during sleep-phase batching.

### 3.5 Hardened API Server (`lumen.api.server`)

The FastAPI server provides a production-oriented surface:

- **Authentication**: API-key middleware via `LUMEN_API_KEY`; all endpoints except `/health` and `/dashboard` require a valid key.
- **Rate limiting**: slowapi with configurable `LUMEN_API_RATE_LIMIT`.
- **CORS**: configurable `LUMEN_ALLOWED_ORIGINS`.
- **Request safety**: body-size limit (default 1 MB), request-ID propagation (`X-Request-ID`), global unhandled-exception handler (no stack traces in responses).
- **Endpoints**: `/v1/search`, `/v1/store`, `/v1/feedback`, `/v1/assemble`, `/v1/turn`, `/v1/status`, `/v1/dashboard-data`, `/health`, `/dashboard`, `/metrics`.

### 3.6 Effectiveness Dashboard

A self-hosted HTML dashboard with real-time palace topology, retrieval metrics, Twin-Force Controller state, forgetting pipeline health, and SOTA comparison — served at `/dashboard` with no external network or CDN dependencies (all assets bundled). Dashboard metrics load dynamically from JSON result files produced by the benchmark harnesses.

---

## 4. Evaluation Methodology

All benchmarks run on single-core CPU environments. Embeddings use `all-MiniLM-L6-v2` (384-dim) or `BAAI/bge-small-en-v1.5` (384-dim) where a real embedder is available; the E2E and retrieval benchmarks use the embedder specified in their respective committed JSON results. Statistical confidence is estimated via bootstrap 95% CIs (1,000 resamples).

### 4.1 Benchmark Suites

| Suite | What It Measures | Configuration |
|---|---|---|
| **Retrieval** | R@k, nDCG@k, MAP, MRR across BM25/dense/hybrid. MS MARCO + synthetic subsets. | 1,000 passages, 50 queries, 3 seeds |
| **Palace Navigation Efficiency (PNE)** | Latency speedup and recall retention of room/locus-constrained vs. flat global search. | 8 rooms × 4 loci × 40 passages = 1,280 chunks; 324 queries |
| **Forgetting** | 90-day survival curves, interference precision, V(m) distribution drift. | 10,000 chunks over 90 days |
| **Performance** | Query latency (p50/p95/p99), ingestion throughput, RAM/DB footprint. | 1K–50K passages |
| **End-to-End (E2E) Memory Quality** | Multi-turn conversational memory persistence with 7 agent personas. | 28 queries across 14 sessions |
| **Component Ablation** | Latency contribution of TFC, V(m), graph, fusion vs. BM25-only and dense-only baselines. | Domain corpus, 20 queries |
| **BEIR Subset Retrieval** | R@k, nDCG@k, MAP, MRR on standard BEIR subsets (NFCorpus, SciFact, FiQA-2018, ArguAna, SCIDOCS) with BM25/dense/hybrid pipelines. | 3,000 passages, 50 queries, 3 seeds per dataset |

> **Statistical Power and Sample-Size Limitations.** Sample sizes across all reported benchmarks are modest: 3 random seeds for retrieval and BEIR subset evaluations, 28 queries for end-to-end memory quality, 41 implicit feedback ratings, and 20 queries for component ablation. Bootstrap confidence intervals primarily reflect resampling noise rather than genuine variance across conditions. All sample-size and statistical-power caveats are consolidated here; they are not repeated in individual subsections.

### 4.2 BEIR Subset Protocol and Predicted Channel Dominance

Lumen's BEIR evaluation harness (`benchmarks/beir/run.py`) evaluates retrieval quality on real document collections from the BEIR benchmark (Thakur et al., 2021). This is a significant methodological advance over the synthetic corpus: the passages and relevance judgments are sourced from published academic datasets with human annotations.

**Datasets.** Five BEIR subsets spanning biomedical (NFCorpus, SciFact), financial QA (FiQA-2018), argument retrieval (ArguAna), and scientific document retrieval (SCIDOCS):

| Dataset | Domain | Full Size | Sampled |
|---|---|---|---|
| NFCorpus | Biomedical abstracts | ~3,600 docs | 3,000 docs |
| SciFact | Scientific claim verification | ~5,000 docs | 3,000 docs |
| FiQA-2018 | Financial question answering | ~57,000 docs | 3,000 docs |
| ArguAna | Argumentative passage retrieval | ~8,600 docs | 3,000 docs |
| SCIDOCS | Scientific document recommendation | ~25,000 docs | 3,000 docs |

**Protocol.** For each dataset: load corpus, queries, and binary relevance judgments via the `datasets` library; embed all passages with `BAAI/bge-small-en-v1.5` (MiniLM evaluation planned for subsequent revision); store in temporary Lumen SQLite databases; evaluate BM25-only (FTS5), Dense-only (sqlite-vec/USearch), and Hybrid (RRF fusion) pipelines across 3 random seeds (42, 123, 456); compute R@k and nDCG@k for k ∈ {1, 3, 5, 10, 20}, plus MAP and MRR, with 95% bootstrap CIs (1,000 resamples); compute an independent `rank-bm25` Python baseline for comparison. The total evaluation surface is 5 datasets × 1 embedder × 3 seeds × 4 retrieval configurations = 60 measurement points (MiniLM adds a second 60-point layer in the planned full-scale run). The full evaluation requires approximately 45–60 minutes on a single CPU core and is run via `python -m benchmarks.beir.run`. The subset produced for v0.2.0-beta used 500 passages and 20 queries per dataset (run with `--max-passages 500 --max-queries 20`). Results are written to `benchmarks/beir/results/beir_results.json` and include R@k and nDCG@k for k ∈ {1, 3, 5, 10, 20} with 95% bootstrap CIs, MAP, MRR, p50 latency per configuration, and an independent per-dataset `rank_bm25` baseline.

**Key methodological improvements over synthetic retrieval benchmarks:**

1. **Real relevance judgments**: Relevance is determined by human annotators, not keyword overlap.
2. **Independent BM25 baseline**: The `rank_bm25` library provides an external reference point, making Lumen's internal BM25 (SQLite FTS5) auditable.
3. **Multi-domain coverage**: Biomedical, financial, scientific, and argument retrieval test different retrieval properties — dense retrieval may excel on semantic paraphrase tasks (ArguAna) while BM25 dominates exact keyword tasks (FiQA).
4. **Full metric suite**: nDCG@k alongside R@k captures ranking quality, not just recall.

**Table 6: Empirical Channel Dominance (BEIR Subset, 500 passages, 20 queries, bge-small-en-v1.5)**

Results from the first BEIR subset evaluation are now available (see `benchmarks/beir/results/beir_results.json`). Because the sample is small (500 passages and 20 queries per dataset), these numbers are directional only and should not be compared directly to full-corpus BEIR leaderboard figures.

| Dataset | Dominant Channel (R@10) | R@10 | nDCG@10 | Key Observation |
|---|---|---|---|---|
| NFCorpus | Dense / Hybrid | 0.3565 | 0.3322 | Dense (0.2782) and hybrid outperform BM25 (0.3235); contrary to the keyword-first hypothesis |
| SciFact | Dense / Hybrid | 0.5000 | 0.4281 | BM25 scores 0.4500 R@10; dense and hybrid both reach 0.5000. Dense leads at R@10 on this small sample. |
| FiQA-2018 | BM25 (R@10) / Dense (R@1) | 0.5000 | 0.4029 | BM25 leads at R@10 (0.5000 vs 0.4500); dense leads at R@1 (0.4000 vs 0.3500) |
| ArguAna | Dense / BM25 | 1.0000 | 0.5200 | BM25 catches up at R@20; dense leads at R@3–R@10 as hypothesised |
| SCIDOCS | Dense / Hybrid | 0.1025 | 0.5848 | Very low absolute recall across all channels on this 500-doc sample |

**Methodology caveats:**

- **Small-N results.** The evaluation used 500 passages and 20 queries per dataset (a convenience sample for single-CPU runtime). Full BEIR corpora range from 3,600–57,000 documents. These numbers establish harness functionality, not leaderboard competitiveness.
- **Dataset complexity varies dramatically.** NFCorpus and SciFact have short, focused queries with narrow relevance scopes. ArguAna queries are argumentative paraphrases. The within-harness comparison across domains is diagnostic of retrieval channel strengths.
- **This is not a BEIR leaderboard submission.** The harness runs the BEIR protocol internally on a sampled subset. A full BEIR leaderboard evaluation requires running on the complete corpus and submitting to `beir-cellar/beir`. This remains future work.
- **The primary contribution is the infrastructure**: Lumen now has a repeatable, automated BEIR evaluation harness that produces auditable JSON results with bootstrap confidence intervals. As the system improves (better embedders, trained rerankers, HNSW indexing), this harness enables regression tracking.

### 4.3 Live Palace State

As of the report date, the reference Lumen palace contains:

- **5 rooms**: `decisions` (38 chunks), `training-state` (14 chunks), `architecture` (4 chunks), `preferences` (1 chunk), `deployments` (1 chunk)
- **58 active chunks**, 0 forgotten, 100% retention rate
- **41 feedback ratings**, 95.1% implicit satisfaction
- **Embedding model**: `BAAI/bge-small-en-v1.5` (loaded)
- **Context budget**: 2,048 tokens
- **TFC state**: `e = 0.50`, `a = 0.50`, `τ = 7.0`, `r = 3`

This live state is substantially smaller than the synthetic benchmark corpora; it represents real conversation-turn data accumulated during development and pilot testing.

---

## 5. Results

### 5.1 Retrieval Quality

**Table 1: Retrieval Benchmark (Synthetic Corpus, 1,000 passages, 50 queries, committed results from `retrieval_results.json`)**

| Embedder | Config | R@10 | MRR | p50 Latency |
|---|---|---|---|---|
| bge-small-en-v1.5 | BM25-only | 0.149 | 1.000 | 1.2 ms |
| bge-small-en-v1.5 | Dense-only | 0.146 | 0.981 | 10.2 ms |
| bge-small-en-v1.5 | Hybrid | 0.147 | 1.000 | 22.3 ms |
| bge-smen-v1.5 | BM25-only | 0.149 | 1.000 | 1.1 ms |
| bge-smen-v1.5 | Dense-only | 0.146 | 0.981 | 135.9 ms |
| bge-smen-v1.5 | Hybrid | 0.149 | 1.000 | 121.5 ms |

On this keyword-overlap synthetic corpus, BM25 dominates dense retrieval, and hybrid matches BM25 while adding semantic coverage. R@10 and MRR values are nearly identical across the two embedding models; this pattern reflects the synthetic corpus's keyword-overlap construction rather than genuine embedder differentiation. These numbers are not directly comparable to MS MARCO or BEIR leaderboard figures; they establish in-harness consistency.

### 5.2 Palace Navigation Efficiency

**Table 2: PNE Benchmark (1,280 chunks, 324 queries, pre-cached embeddings, committed results from `navigation_results_MiniLM-L6-v2.json` and `navigation_results_bge-smen-v1.5.json`)**

| Strategy | Embedder | p50 Latency | R@10 | Retention vs Global | Speedup |
|---|---|---|---|---|---|
| Global | MiniLM | 22.8 ms | 0.183 | — | 1.00× |
| Oracle Room | MiniLM | **16.3 ms** | 0.183 | **0.998** | **1.50×** |
| Routed Room | MiniLM | **16.5 ms** | 0.184 | 1.002 | **1.50×** |
| Global | BGE-small | 30.2 ms | 0.173 | — | 1.00× |
| Oracle Room | BGE-small | **24.0 ms** | 0.172 | **0.999** | **1.32×** |
| Routed Room | BGE-small | **23.9 ms** | 0.172 | 1.000 | **1.35×** |

Room-constrained search provides 1.32–1.50× latency reduction by pruning ~80% of cross-domain candidates, with >99.8% recall retention. Routing accuracy is an upper bound (queries were constructed with unambiguous room keywords); real-world routing would require a trained classifier.

### 5.3 Forgetting Simulation

Over 90 simulated days with default `τ_h = 7`, all 10,000 synthetic chunks decay to mean V(m) below 0.05. This confirms that **the default forgetting pipeline performs unconditional decay, not selective curation.** The L2 interference layer correctly identifies high-similarity pairs (cos > 0.85) and applies penalties. L3 budget eviction respects the degradation chain. Without a re-access reinforcement boost, no memory survives indefinitely at default settings. **This is the primary gating item for any production-readiness claim: the current system forgets everything by design.**

### 5.4 End-to-End Memory Quality

**Table 3: E2E Results (7 personas, 28 queries, mock embedder, committed results)**

| Metric | Mean | 95% CI |
|---|---|---|
| Recall@1 | **0.036** | [0.000, 0.107] |
| Recall@5 | **0.786** | [0.643, 0.929] |
| Recall@10 | **1.000** | [1.000, 1.000] |
| Avg Latency (ms) | **5.3** | [4.7, 5.8] |

> **Methodological caveat.** The committed E2E benchmark results use a MockEmbedder (deterministic random vectors) rather than a real embedding model. This means semantic similarity scores are deterministic but not meaningful. The 100% R@10 reflects the benchmark's low-cardinality structure (6 facts per persona with 1–2 expected fact IDs per query) combined with the mock embedder's deterministic behavior. A re-run with BGE-small or MiniLM embeddings is needed before these numbers can be interpreted as genuine retrieval quality. This validates *exact-match factual recall in small pools* at R@10 — a "needle retrieval" task — not general IR robustness.

### 5.5 Live Feedback Satisfaction

From 41 implicit feedback ratings on the reference palace:

- **Implicit feedback satisfaction**: 95.1%
- **Positive samples**: 39
- **Negative samples**: 2

This is a self-reported dashboard metric derived from the `/feedback` endpoint, not an independent human evaluation. Directional only; see Section 4 for consolidated sample-size limitations.

### 5.6 Cross-System Resource Comparison

A cross-system resource comparison table is provided in Appendix A. **Important caveat:** Lumen numbers are measured directly via psutil RSS and benchmark harness; competitor numbers are sourced from their published documentation, which may reflect different hardware, embedders, and corpus sizes. The table is provided for rough orientation only. A shared-harness evaluation with identical embedders is pending and should supersede the appendix table when available.

### 5.7 Component Ablation

**Table 5: Ablation Study (Domain Corpus, 20 queries, MiniLM, committed results)**

| Configuration | Room Acc. | Locus Acc. | Keyword Recall | Latency (ms) |
|---|---|---|---|---|
| FULL | 1.000 | 1.000 | 0.961 | 47.7 |
| NO_TFC | 1.000 | 1.000 | 0.961 | 66.1 |
| NO_VM | 1.000 | 1.000 | 0.961 | 142.6 |
| NO_GRAPH | 1.000 | 1.000 | 0.961 | 88.0 |
| BM25_ONLY | 1.000 | 1.000 | 0.948 | **2.2** |
| DENSE_ONLY | 1.000 | 1.000 | 0.961 | 31.0 |

On this 111-chunk corpus, all configurations hit ceiling accuracy. The ablation's value is latency decomposition: FULL adds 16.7 ms overhead beyond DENSE_ONLY (fusion + reranking + graph + TFC + V(m)). BM25-only is 14.0× faster, confirming that on small lexically-aligned corpora, the dense channel adds overhead without accuracy gain. Whether hybrid improves accuracy on larger, semantically harder corpora remains untested.

### 5.8 BEIR Subset Evaluation

The BEIR subset evaluation harness is described in Section 4.2. First empirical results on 500-passage / 20-query subsets are now available (see Table 6 and `benchmarks/beir/results/beir_results.json`). Full-corpus leaderboard-scale evaluation remains future work.

---

## 6. Known Limitations and Resolved Issues

| # | Limitation | Status | Detail |
|---|---|---|---|
| 1 | All evaluation on synthetic corpora. | **Partially addressed** | Domain corpus (111 hand-crafted chunks) and BEIR subset harness (5 datasets) are now evaluated, but results are still sampled (3,000 docs) rather than full-corpus, and no human relevance judgment study has been conducted. |
| 2 | Forgetting performs unconditional decay. | **Partially addressed** | Re-access reinforcement is implemented (multiplicative V(m) boost on retrieval, fresh `last_access_at`). Selective retention now works in principle; weight learning from implicit feedback remains unprioritised at scale. |
| 3 | V(m) not validated against human preferences. | **Partially addressed** | 41 implicit feedback samples yield 95.1% satisfaction, but the Nelder-Mead weight-learning pathway has not been exercised at scale. |
| 4 | TFC hand-tuned without sensitivity analysis. | **Partially addressed** | TFC sensitivity grid-search results exist (81 configurations, 99.6% R@10 across all). Thresholds remain heuristic; RL or Bayesian optimization not yet applied. |
| 5 | PNE routing is an upper bound. | **Partially addressed** | Keyword-based router remains default, but an embedding-aware logistic regression classifier (`IntentRouter.train_lr`) is implemented and can be persisted/loaded. No automatic training pipeline yet. |
| 6 | Small-N bootstrap CIs. | **Open** | See Section 4 consolidated callout (3 seeds, 28 queries, 41 ratings, 20 ablation queries). |
| 7 | Cross-system comparison is heterogeneous. | **Open** | Shared harness with identical embedders still pending. |
| 8 | Multi-user, encryption-at-rest, P2P not implemented. | **Partially addressed** | Beam P2P is implemented but not hardened for adversarial networks. Multi-user tenant ID support is present in API and schema. Encryption-at-rest is deferred to v0.3.0; use OS-level disk encryption as a stopgap. |
| 9 | Optical degradation not validated per level. | **Partially addressed** | Optical degradation results exist for FP32/FP16/INT8/BINARY; dense recall degrades from 1.0→0.422 R@10. The optical-level increment bug is fixed. |
| 10 | BM25 fails on punctuation-heavy queries. | **Fixed** | FTS5 lexical search now handles apostrophes, periods, and punctuation via query sanitization. |
| 11 | L2 interference skipped in sqlite-vec-only mode. | **Fixed** | Interference checker now reads from `vec_chunks` when `vec_fallback` is empty. |
| 12 | USearch backend crashes when unavailable. | **Fixed** | Graceful fallback to brute-force `FakeSqliteVecBackend` with logged warning. |
| 13 | Dashboard metrics hardcoded. | **Fixed** | Dashboard now loads benchmark values dynamically from JSON result files. |

---

## 7. Discussion

### 7.1 What This Work Demonstrates

This release demonstrates that a SQLite-based memory palace is not merely a research curiosity but a **usable, integrable component** in real agent stacks. The LangGraph checkpoint saver and MCP server are not wrappers around external APIs; they are first-class adapters that let Lumen replace cloud-based memory and checkpoint services entirely. The PNE benchmark confirms that topological structure delivers measurable latency wins with negligible recall loss. The E2E benchmark confirms that multi-turn factual recall works in controlled settings.

### 7.2 What Remains Open

We continue to lack:
- Large-scale retrieval evaluation on real document collections (full BEIR/MTEB submission at leaderboard scale).
- A BEIR subset harness now evaluates NFCorpus, SciFact, FiQA-2018, ArguAna, and SCIDOCS with standard metrics on a sampled 500-doc, 20-query slice. A full 3,000-doc evaluation is planned for the next revision.
- Validation that the forgetting pipeline preserves high-value memories selectively at scale (re-access reinforcement is implemented but not yet validated with longitudinal human studies).
- Evidence that the hybrid pipeline outperforms BM25-only on semantically hard queries.
- An automatically trained intent router; the embedding-aware LR classifier exists but requires manual training and has no automatic retraining pipeline.
- Production-hardened P2P security (Beam is household-trusted only).
- Encryption-at-rest field-level benchmarks; SQLCipher support is implemented but retrieval accuracy on encrypted databases has not been independently measured.

### 7.3 Honest Claim Calibration

We maintain an explicit posture of claim deflation. The V(m) factors are shallow lexical heuristics, not validated psychological measurements. The TFC thresholds are hand-tuned without sensitivity analysis. The additions (integrations, benchmarks, API hardening) improve *usability* and *robustness*; they do not yet validate *superiority* over flat vector databases on general retrieval tasks. The honest claim is: **Lumen is a feature-complete beta framework for sovereign agentic memory with unique lifecycle management and native integrations; its retrieval quality is competitive in controlled settings but not yet validated at leaderboard scale.**

---

## 8. Conclusion

Lumen advances agentic memory from research concept to beta usability. The core contribution is architectural: a structured memory palace with heuristic forgetting, operating entirely on-device, plugged into LangGraph, LangChain, MCP, and FastAPI ecosystems. Closed engineering gaps include API hardening, punctuation-safe BM25, sqlite-vec interference coverage, USearch graceful fallback, and dynamic dashboard metrics. Partially addressed gaps include a hand-crafted domain corpus, implicit feedback learning, P2P sharing via Beam, corrected optical degradation transitions, and — critically — a BEIR subset evaluation harness that replaces synthetic-only benchmarks with real relevance judgments. Remaining open work includes a re-access reinforcement mechanism for selective forgetting, a trained intent-to-room router, and encryption-at-rest for the SQLite database.

The system remains Apache 2.0–licensed open-source software. The complete source code (64 modules, ~8.4K lines of Python, 273 tests), documentation, eleven benchmark suites (including BEIR, optical degradation, and TFC sensitivity), domain corpus, and integrations are available at `https://github.com/QuantumindSSI/lumen`.

---

## References

1. Bajaj, P., et al. (2016). MS MARCO. *arXiv:1611.09268*.
2. Cormack, G. V., et al. (2009). Reciprocal rank fusion. *SIGIR '09*.
3. Ebbinghaus, H. (1885). *Memory: A contribution to experimental psychology*.
4. Karpukhin, V., et al. (2020). Dense passage retrieval. *EMNLP 2020*.
5. Lewis, P., et al. (2020). Retrieval-augmented generation. *NeurIPS 2020*.
6. Liu, N. F., et al. (2024). Lost in the middle. *TACL 2024*.
7. Muennighoff, N., et al. (2023). MTEB. *EACL 2023*.
8. Packer, C., et al. (2023). MemGPT. *arXiv:2310.08560*.
9. Park, J. S., et al. (2023). Generative agents. *UIST 2023*.
10. Santhanam, K., et al. (2022). ColBERTv2. *NAACL 2022*.
11. Thakur, N., et al. (2021). BEIR. *NeurIPS 2021*.
12. Vaswani, A., et al. (2017). Attention is all you need. *NeurIPS 2017*.
13. Wang, L., et al. (2024). A survey on LLM-based autonomous agents. *Frontiers of CS*.

---

*QuantumindSSI — July 2026 — Apache 2.0 License*

---

## Appendix A: Cross-System Comparison (Deferred)

**Status.** A shared-harness cross-system comparison with identical embedders, hardware, and corpus is planned for v0.3.0. The committed `cross_system_results.json` is currently empty (`[]`) — no system-to-system comparison benchmarks have been executed. The qualitative feature comparison below reflects whether each feature exists in the respective system (by code inspection or published documentation), not a performance ranking.

**Table A2: Qualitative Feature Comparison (Existence, Not Performance)**

| Feature | Lumen | Chroma | FAISS | Qdrant |
|---|---|---|---|---|
| Graceful forgetting | Yes | No | No | No |
| Offline operation | Yes | Partial | Yes | No |
| LangGraph checkpoint | Yes | No | No | No |
| MCP server | Yes | No | No | No |

Quantitative comparisons (RAM, latency, storage, cold start) against Chroma, FAISS, and Qdrant on identical hardware with identical embedders are deferred to v0.3.0.
