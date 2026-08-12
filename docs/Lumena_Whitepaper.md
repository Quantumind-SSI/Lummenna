# Lumena: A Sovereign Agentic Memory Framework

**Local-first memory and context architecture for autonomous AI agents**

**Version:** v1.0.0  
**Organization:** QuantumindSSI Research  
**License:** Apache 2.0  
**Date:** August 2026

---

## Abstract

Lumena is an open-source, local-first memory framework that gives large language model (LLM) agents persistent, structured, and lifecycle-aware memory without cloud dependencies. Unlike conventional vector databases that treat memory as a flat collection of vectors, Lumena organizes information as a *memory palace* — a structured topology of rooms, loci, and corridors governed by a heuristic *Twin-Force Controller* that balances mnemonic conservation against contextual attention.

The system operates entirely on-device via ONNX Runtime and SQLite, eliminating external API costs and preserving data sovereignty. Lumena provides native integrations for LangGraph, LangChain, MCP-compatible agents (OpenCode, Claude Desktop), and a production-hardened FastAPI REST server. At v1.0.0, topology-constrained search prunes ~80% of the corpus per query with ≥99.7% recall retention (routed ≥100% at all measured k; oracle worst case 99.69% at k=5) and a modest ~1.08× mean-latency improvement over flat global search (navigation benchmark; earlier 1.32–1.50× figures came from a biased mean-of-ratios statistic and have been retracted). Lumena ships with eleven benchmark suites spanning retrieval quality, forgetting behavior, navigation efficiency, and stress testing — eight with committed result artifacts, three (perf, stress, cross-system) with harnesses awaiting runs.

---

## 1. The Agentic Memory Problem

Modern LLM agents are capable of multi-turn reasoning, tool use, and long-horizon task completion — yet their effective working memory is bounded by context windows that range from 4K to 2M tokens. Simple append-only conversation buffers quickly saturate this window with irrelevant history, degrading agent performance and increasing inference cost.

Retrieval-Augmented Generation (RAG) partially addresses this by fetching external documents, but existing approaches treat memory as an unstructured flat collection. They lack:

- **Lifecycle management:** Memories are inserted and deleted, never born, ranked, decayed, or curated.
- **Spatial organization:** Flat vector search wastes computation on irrelevant domains.
- **Value ranking:** No system learns which memories matter to a specific user.
- **Data sovereignty:** Most solutions require persistent cloud connections and external embedding APIs.

The three dominant patterns in 2026 — pure vector stores (Chroma, FAISS, Qdrant), cloud-hosted agent memory services (MemGPT/Letta, Zep, Mem0), and RAG orchestrators (LlamaIndex, LangChain) — share these limitations to varying degrees.

Lumena was designed to close these gaps.

---

## 2. What Lumena Is

Lumena is a **production-grade, open-source agentic memory framework** with the following defining properties:

| Property | What It Means |
|---|---|
| **Sovereign** | All computation — embedding, storage, retrieval, consolidation — runs on-device. No cloud APIs, no telemetry, no external dependencies. |
| **Structured** | Memories live in a spatial topology (rooms → loci → chunks) rather than a flat vector list. |
| **Lifecycle-aware** | Memories decay, interfere, degrade in precision, and are released — mirroring biological memory. |
| **Integrable** | Native adapters for LangGraph, LangChain, MCP, and FastAPI REST plug into existing agent stacks without lock-in. |
| **Lightweight** | Single-file SQLite database; embedding model is a 33 MB INT8 ONNX export. RAM/storage footprints have not yet been benchmarked (perf suite pending). |

At its core, Lumena is not merely a vector database with extra features. It is a **complete memory substrate** for agents: storage, retrieval, ranking, forgetting, consolidation, and cross-agent sharing — all in one system.

---

## 3. Architecture Overview

### 3.1 The Memory Palace

Lumena structures memory as a graph with three entity types:

- **Rooms** (8–50 per palace): Top-level categories such as `decisions`, `architecture`, `preferences`, or `healthcare_ai`. Each room has a domain type (`domain`, `project`, `person`, `ephemeral`).
- **Loci** (4–20 per room): Sub-locations acting as semantic clusters, each maintaining a cached vector centroid for rapid orientation.
- **Chunks** (per locus): Atomic memory units containing raw text, a 384-dimensional embedding vector, a learned value score V(m), bi-temporal validity markers, and a resolution quantifier (`FP32 → FP16 → INT8 → BINARY → RELEASED`).

This topology is stored in a single SQLite database with WAL mode, FTS5 full-text indexing, and vector virtual tables. The entire database is one portable file.

### 3.2 The Twin-Force Controller

Human cognition balances two forces: the drive to preserve existing knowledge (mnemonic conservation) and the drive to attend to new information (contextual attention). The Twin-Force Controller (TFC) maintains this equilibrium through four state variables:

| Variable | Role | Range |
|---|---|---|
| `e` | Mnemonic conservation bias | [0, 1] |
| `a` | Attentional temperature | [0, 1] |
| `τ` | Temporal horizon | > 0 days |
| `r` | Resolution level | {0, ..., 5} |

The TFC updates deterministically from interaction signals: novelty drives exploration (lower `e`, higher `a`), repetition drives consolidation (higher `e`, lower `a`), and memory pressure triggers resolution reduction to maintain performance.

### 3.3 Multi-Channel Retrieval

Retrieval proceeds through five stages:

1. **Intent Classification** — Rule-based router classifies queries as factual, exploratory, relational, or temporal.
2. **Parallel Retrieval** — Three channels execute concurrently:
   - *Lexical (BM25)*: SQLite FTS5 with Porter stemming
   - *Dense (Vector)*: sqlite-vec or USearch HNSW with cosine distance
   - *Graph (Structural)*: BFS traversal from top dense-hit seeds across provenance links
3. **Reciprocal Rank Fusion** — Combines channel results with `k_RRF = 60`.
4. **Multi-Factor Reranking** — Final scores incorporate value V(m), vector similarity, recency, and goal relevance.
5. **Repair Loop** — If results are empty, the pipeline re-executes with relaxed constraints.

### 3.4 The V(m) Value Model

Every chunk receives a scalar value `V(m) ∈ [0, 1]` computed from seven heuristic factors:

- **Goal relevance** (0.20): Overlap with active user goals
- **Value alignment** (0.15): Overlap with user-defined value keywords
- **Self relevance** (0.15): Density of first-person pronouns
- **Task utility** (0.15): Presence of action verbs
- **Emotional intensity** (0.15): Lexicographic sentiment polarity
- **Reliability** (0.10): Source-type weighting (user_input > agent_reasoning > import)
- **Usage history** (0.10): Frequency and recency of access

At ≥10 feedback samples, Nelder-Mead optimization adapts the weight vector to maximize the separation between positively and negatively rated chunks.

### 3.5 The Forgetting Pipeline

Four layers of heuristic forgetting operate at different timescales:

- **L1 — Exponential Decay (daily):** Ebbinghaus-inspired passive decay with configurable half-life (default 7 days).
- **L2 — Interference Weakening (on write):** New chunks penalize semantically similar existing residents within the same locus.
- **L3 — Budget Eviction (periodic):** When memory pressure exceeds 85% of budget, low-value chunks degrade through the precision chain (`FP32 → FP16 → INT8 → BINARY → RELEASED`).
- **L4 — Compliance Deletion (on write):** PII patterns trigger redaction and immediate release.

This is not a bug or an afterthought. Forgetting is a first-class design principle: agents that remember everything eventually drown in noise.

---

## 4. Integrations and Ecosystem

Lumena is designed to plug into existing agent stacks, not replace them.

### 4.1 LangGraph

`LumenaCheckpointSaver` implements LangGraph's `BaseCheckpointSaver` protocol, enabling sovereign graph-agent state persistence without external checkpoint services. Each `thread_id` receives its own locus inside a dedicated `langgraph` room. `LumenaGraphStore` provides cross-thread long-term memory, allowing agents to reference facts learned in one thread while executing another.

### 4.2 Model Context Protocol (MCP)

The MCP server exposes seven operations to OpenCode, Claude Desktop, and GitHub Copilot via stdio transport:

- `lumena_search` — hybrid semantic + lexical search
- `lumena_store` — persist a memory with room/locus/source-type
- `lumena_assemble` — retrieve and assemble context in one call
- `lumena_turn` — store a conversation turn and log implicit feedback
- `lumena_feedback` — rate a retrieved memory's usefulness
- `lumena_status` — palace overview with room/chunk counts and TFC state
- `lumena_dashboard` — real-time effectiveness dashboard

This enables any MCP-compatible agent to read from and write to the user's local Lumena palace without custom API clients.

### 4.3 Beam P2P Sharing

Beam is a household-local memory sharing protocol for trusted LAN environments. Multiple Lumena instances (e.g., a Raspberry Pi agent and a laptop agent) can share memories with:

- Mandatory AES-256-GCM encryption
- HMAC-SHA256 integrity signing with 30-second replay protection
- Zeroconf service discovery (`_lumena-beam._tcp`)
- Automatic permission decay on shared memories

Plaintext transport requires an explicit `LUMENA_P2P_INSECURE=1` flag and is intended for testing only.

### 4.4 Production API Server

The FastAPI server provides a hardened REST surface:

- API-key authentication via `LUMENA_API_KEY`
- Rate limiting via slowapi
- CORS middleware with configurable origins
- Request-size limits, request-ID propagation, and a global unhandled-exception handler
- Self-hosted HTML dashboard with real-time palace topology, retrieval metrics, TFC state, and benchmark comparisons — no external CDN dependencies

### 4.5 LangChain and Python SDK

`LumenaChatMemory` provides a LangChain-compatible memory adapter for conversational agents. The Python SDK (`ConversationMemory`) supports direct programmatic use:

```python
from lumena.conversation import ConversationMemory
memory = ConversationMemory(config=config, conn=conn)
context = memory.retrieve_and_assemble("What UI settings does the user like?")
```

---

## 5. Benchmarks at a Glance

Lumena ships with eleven benchmark suites. Eight have results committed to the repository as reproducible JSON artifacts; the performance, stress, and cross-system suites are harnesses whose results are not yet committed.

| Suite | What It Measures | Key Result |
|---|---|---|
| **Retrieval Quality** | R@k, nDCG, MAP, MRR across BM25 / dense / hybrid | Hybrid matches BM25 on synthetic corpus; BEIR subset evaluated on 5 real datasets |
| **Palace Navigation Efficiency** | Speedup of room-constrained vs. flat global search | ~80% corpus pruning, ≥99.7% recall retention (oracle worst case 99.69% @ k=5), ~1.08× mean-latency speedup (ratio of means; earlier 1.32–1.50× mean-of-ratios figures retracted as statistically biased) |
| **Forgetting Simulation** | 90-day survival curves, interference precision | All synthetic chunks decay below threshold at default settings; re-access reinforcement implemented |
| **End-to-End Memory Quality** | Multi-turn conversational recall (7 personas, 28 queries) | 100% R@10, 100% R@5 (real MiniLM embedder; embedding-similarity self-grading of verbatim-stored facts — a smoke test, not an external quality claim) |
| **Component Ablation** | Latency contribution of TFC, V(m), graph, fusion | Dense-only 10.6 ms; ablated pipelines ~12 ms; FULL config 78.8 ms in the committed run (FULL is measured first and likely absorbs warm-up — treat latency deltas as indicative). Accuracy was identical across ALL ablation configs — the corpus is too easy to discriminate quality contributions |
| **BEIR Subset** | Real-document retrieval (NFCorpus, SciFact, FiQA, ArguAna, SCIDOCS) | Harness validated on 500-doc/20-query subsets; full corpus pending |
| **Optical Degradation** | Retrieval accuracy at FP32/FP16/INT8/BINARY | Dense recall degrades from 1.0 → 0.422 R@10 across precision levels (synthetic random-vector substrate; isolates quantization error) |
| **TFC Sensitivity** | Grid search across controller parameters | 99.6% R@10 stability across 81 TFC configurations (synthetic random-vector substrate) |
| **Stress Test** | 100K+ ingestion, multi-threaded throughput | Harness ready; no committed results yet |

> **Honest caveat:** Sample sizes are modest (3 seeds for retrieval, 28 queries for E2E). The synthetic retrieval corpus is deliberately easy and near-saturates nDCG; the component-ablation suite showed **no accuracy differences between configurations** on its 111-chunk corpus (only latency differences are reportable — the corpus is too small to discriminate quality contributions). Bootstrap confidence intervals reflect resampling noise. Full BEIR/MTEB leaderboard submission remains future work.

---

## 6. Use Cases

Lumena is designed for scenarios where memory must be persistent, private, and structured:

| Scenario | How Lumena Helps |
|---|---|
| **Personal AI Assistant** | Remembers preferences, facts, and conversation history across sessions without cloud dependence. |
| **Air-Gapped Enterprise Systems** | Provides RAG-style retrieval in environments with no external network access. |
| **Embedded / IoT Agents** | Runs on Raspberry Pi 5, Jetson, and Orange Pi with wear-aware batch writing for SD/eMMC endurance. |
| **Research and Provenance** | Bi-temporal validity markers and provenance chains track when knowledge was acquired and superseded. |
| **Multi-Agent Households** | Beam P2P enables multiple local agents to share a common memory substrate. |
| **Compliance-Sensitive Applications** | Automated PII scanning, safety-triggered deletion, and structured audit logging support GDPR/CCPA workflows. |

---

## 7. Security and Sovereignty

Lumena is sovereign-by-default:

- `LUMENA_SOVEREIGN=true` (default) blocks all external network calls, P2P sharing, and model downloads.
- Embeddings run locally via ONNX Runtime (`bge-small-en-v1.5`, 384 dimensions, <100 MB download).
- PII scanning runs at the write path with configurable block/redact/hash strategies.
- File permissions are enforced (`~/.lumena` at `700` / `600`).
- Audit logging records all operations to the SQLite `audit_log` table.

**Known limitation:** Encryption-at-rest is not yet field-validated. SQLCipher schema support exists; use OS-level disk encryption (LUKS, FileVault, BitLocker) as a stopgap. Production-hardened P2P for adversarial WAN environments is deferred to v0.3.0.

---

## 8. Development Status and Roadmap

Lumena is **stable software** — API-stable, actively developed, and suitable for production, evaluation, development, and trusted-LAN deployments.

### What Works Today

- End-to-end memory lifecycle: store → search → assemble → feedback → forget
- 294 tests across 41 test files with ~74% line coverage
- FastAPI server with auth, rate limiting, and CORS
- MCP server with 7 tools
- LangChain and LangGraph native adapters
- Real-time effectiveness dashboard
- P2P memory sharing on trusted LANs

### What Remains for v1.1.0

- Full-corpus BEIR/MTEB leaderboard submission
- Validated encryption-at-rest with SQLCipher
- Production-hardened P2P transport encryption
- Automatically trained intent router (embedding-aware LR classifier exists but requires manual training)
- Longitudinal validation of selective forgetting at scale
- Cross-system quantitative comparison on identical hardware and embedders

---

## 9. Conclusion

Lumena advances agentic memory from research concept to production-ready usability. Its core contribution is architectural: a structured memory palace with heuristic forgetting, operating entirely on-device, natively integrated into LangGraph, LangChain, MCP, and FastAPI ecosystems.

The honest claim is not that Lumena surpasses every vector database on every retrieval metric. The honest claim is that **no existing system combines structured topology, lifecycle management, learned value ranking, and sovereign deployment in a single integrable framework** — and that Lumena demonstrates this combination is feasible, usable, and directionally performant.

The complete source code (~9K lines of Python across 65 modules), documentation, eleven benchmark suites, domain corpus, and integrations are available at:

> **https://github.com/QuantumindSSI/lumena**

Released under the Apache 2.0 license.

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

*QuantumindSSI — August 2026 — Apache 2.0 License*

*Local-first memory for your agents. On your hardware. Your way.*
