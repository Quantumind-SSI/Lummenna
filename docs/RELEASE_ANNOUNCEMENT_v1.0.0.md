# Lumena v1.0.0 — Inaugural Public Release

**Release Date:** 7 August 2026  
**Version:** v1.0.0  
**Repository:** https://github.com/QuantumindSSI/lumena  
**License (at release):** Apache 2.0  

> **Note:** This is the historical v1.0.0 announcement. Lumena is now
> **dual-licensed** — AGPL-3.0-or-later (Community) or a commercial license from
> QuantumindSSI (Pro). See `LICENSE` and `COMMERCIAL-LICENSE.md`. The Apache 2.0
> grant on releases up to and including v1.0.0 remains irrevocable.

---

## 1. Executive Summary

Today we announce **Lumena v1.0.0**, the inaugural public release of our local-first memory and context framework for sovereign AI agents. Lumena gives LLM agents persistent, structured, searchable memory—without sending a single byte to the cloud. It runs entirely on your hardware, stores everything in a single SQLite file, and generates embeddings locally via ONNX Runtime.

This release marks the transition from beta to **production-ready**, with an API-stability commitment, 287 passing tests, 74 % coverage, and a fully documented security posture.

---

## 2. What Is Lumena?

Most AI agents today are stateless. Every session starts from scratch. The few that remember rely on remote vector databases, managed cloud services, or opaque SaaS pipelines.

Lumena is different.

Lumena is a **local-first memory palace** for agents. It stores memories as structured *chunks* inside *rooms* and *loci*, retrieves them through a hybrid pipeline (BM25 lexical search + dense vector similarity + optional graph traversal), and actively manages their lifecycle with a three-layer forgetting model using decay, interference, and eviction.

Your agents remember. Your data never leaves your machine.

---

## 3. What’s New in v1.0.0

### New Integrations
- **LangGraph checkpoint saver** — persist graph state and cross-thread long-term memory natively.
- **LangChain memory adapter** — drop-in `LumenaChatMemory` for any LangChain application.
- **MCP server** — 7 tools exposed to OpenCode, Claude Desktop, and any MCP-compatible client.
- **FastAPI REST API** — versioned (`/v1/`) endpoints with auth, rate limiting, CORS, and security headers.

### New Benchmarks
- **End-to-End Memory Quality** — 28 conversational queries across 7 personas, measuring recall and latency via embedding-similarity self-grading (a persistence smoke test, not an external quality comparison).
- **Palace Navigation Efficiency (PNE)** — room/locus topology prunes ~80% of the corpus per query with ≥99.9% recall retention and ~1.08× mean-latency speedup vs. flat global search (results available). *Correction: earlier announcements cited 1.32–1.50×, computed as a per-query mean of ratios, which is biased upward; the honest ratio-of-means is ~1.08×.*
- **Optical Degradation** — retrieval quality validated at FP32, FP16, INT8, and BINARY quantization levels (results available).
- **TFC Sensitivity** — grid search justifying default Twin-Force Controller parameters (81 configurations, results available).
- **BEIR Subset Evaluation** — first real-world retrieval benchmarks on 500-document subsets of NFCorpus, SciFact, FiQA-2018, ArguAna, and SCIDOCS (results available).

### New Datasets & Tooling
- **Domain Corpus** — 111 curated fact chunks across 8 rooms (ML, NLP, cybersecurity, distributed systems, healthcare AI, quantum computing, open source, climate tech) with a one-command seed script.
- **Centralized logging** — structlog-first, replacing every silent `except: pass` in the codebase.
- **PGP key** for `security@lumena.ai` published in `SECURITY.md`.

### Critical Fixes
- USearch backend now gracefully falls back to brute-force when unavailable.
- L2 interference correctly reads embeddings in sqlite-vec-only deployments.
- API `/turn` endpoint resolves real database values instead of placeholders.
- FTS5 lexical search fixed for punctuation-rich queries.

---

## 4. Architecture at a Glance

```
User Input → Intent Router → Parallel Retrieval (BM25 + Dense + Graph)
                                   │
                                   ▼
                           RRF Fusion × V(m) × Recency
                                   │
                                   ▼
                           Context Assembly (Jinja2)
                                   │
                                   ▼
                     Consolidation → Decay / Interference / Eviction
```

- **Hybrid retrieval** — BM25 (SQLite FTS5), cosine-similarity vector search, and optional graph traversal fused with Reciprocal Rank Fusion.
- **Managed forgetting** — L1 time-based decay (Ebbinghaus), L2 similarity interference, L3 budget eviction with optical degradation.
- **Local embeddings** — ONNX Runtime, no API keys, no network calls.
- **Single-file storage** — SQLite with WAL, FTS5, bi-temporal tracking, and provenance chains.

---

## 5. Project Health

| Dimension | Status |
|---|---|
| Tests | 287 passing, 7 skipped |
| Coverage | 74 % |
| Storage | SQLite + WAL + FTS5 |
| Retrieval | BM25 + dense + graph + RRF |
| Forgetting | L1 / L2 / L3 working |
| PII detection | Regex-based, configurable block/redact/hash |
| Audit logging | SQLite table + request tracing |
| API server | FastAPI, versioned, auth, rate-limited |
| MCP server | 7 tools |
| LangChain | `LumenaChatMemory` adapter |
| LangGraph | `LumenaCheckpointSaver` + `LumenaGraphStore` |

---

## 6. Quick Start

```bash
git clone https://github.com/QuantumindSSI/lumena.git
cd lumena
pip install -e ".[dev]"
lumena init --device generic
lumena serve
# Dashboard → http://localhost:8848/dashboard
# API docs  → http://localhost:8848/docs
```

---

## 7. Social Media Kit

### LinkedIn Post (Long Form)

> 🚀 We just shipped **Lumena v1.0.0** — a local-first memory framework for AI agents.
>
> Most agents forget everything when you close the tab. The ones that remember usually send your data to someone else’s cloud.
>
> Lumena fixes both.
>
> It gives LLM agents a structured, searchable, long-term memory palace that runs entirely on your own hardware. No cloud vector DBs. No API keys for storage. No telemetry.
>
> What’s inside:
> • Hybrid retrieval (BM25 + dense + graph) with RRF fusion
> • Three-layer forgetting using decay, interference, and eviction
> • LangGraph, LangChain, MCP, and FastAPI integrations
> • 294 tests; stress harness for 100k+ chunks (no committed results yet)
> • Single-file SQLite, ONNX embeddings, Apache 2.0
>
> This is our production-ready debut. Encryption-at-rest and full BEIR leaderboard runs remain in active development, but the API is stable, the architecture is solid, and the community is growing.
>
> If you believe AI agents should remember without sacrificing sovereignty, give Lumena a try.
>
> 👉 https://github.com/QuantumindSSI/lumena
> 📖 Whitepaper: `docs/Lumena_Whitepaper.md`
> 💬 Matrix: `#lumena:matrix.org`
>
> #AI #LLM #OpenSource #AgenticAI #Memory #LocalFirst #Privacy #LangChain #LangGraph #MCP #Release

---

### Twitter / X Thread (5 Tweets)

**Tweet 1 — Hook**
> 🚀 Introducing Lumena v1.0.0 — local-first memory for AI agents.
>
> Your agents remember. Your data never leaves your machine.
>
> Thread ↓
>  
> https://github.com/QuantumindSSI/lumena

**Tweet 2 — The Problem**
> Most AI agents are stateless. The few that “remember” rely on cloud vector DBs, managed APIs, or opaque SaaS pipelines.
>
> That means: API keys, egress costs, and data you don’t control.

**Tweet 3 — The Solution**
> Lumena is a memory *palace* that runs entirely on your hardware.
>
> • SQLite + ONNX — zero cloud deps
> • BM25 + dense + graph retrieval
> • Human-inspired forgetting (decay, interference, eviction)
> • LangChain, LangGraph, MCP, FastAPI integrations

**Tweet 4 — Proof**
> We’re production-ready — and we’re not fragile.
>
> 294 tests. Stress harness for 100k+ chunks (no committed results yet). 74 % coverage. API-stable.
>
> Honest gaps documented (encryption-at-rest is next). No hype, just code.

**Tweet 5 — CTA**
> Try it in 4 commands:
> ```bash
> git clone https://github.com/QuantumindSSI/lumena.git
> cd lumena && pip install -e ".[dev]"
> lumena init --device generic && lumena serve
> ```
>
> Dashboard opens at localhost:8848.
>
> Star the repo, open an issue, or join `#lumena:matrix.org`.
>
> #LocalFirst #AgenticAI #OpenSource

---

### Short-form Blurbs (Twitter / Mastodon / Bluesky)

**Blurbs 1 — Technical**
> Lumena v1.0.0 is out: a local-first memory palace for LLM agents. BM25 + dense + graph retrieval, three-layer forgetting, and native LangChain / LangGraph / MCP integrations. Zero cloud dependencies. 294 tests. Apache 2.0. → github.com/QuantumindSSI/lumena

**Blurbs 2 — Values-driven**
> We built Lumena because AI agents deserve memory without surveillance. Local embeddings, single-file SQLite, sovereign data. Production-ready today. Check it out → github.com/QuantumindSSI/lumena

**Blurbs 3 — One-liner**
> Lumena: local-first memory for sovereign AI agents. Now in v1.0.0. github.com/QuantumindSSI/lumena

---

## 8. Assets Needed

| Asset | Suggested Spec | Status |
|---|---|---|
| Hero graphic / GIF | 1200×630 px, under 5 MB | *Create before publishing* |
| Architecture diagram | 1200×1200 px, dark mode friendly | *Reuse README ascii art or design SVG* |
| Demo video (30–60 s) | 1080p, silent captioned | *Nice-to-have* |
| OG image for repo | `repository-open-graph-image.png` in `.github/` | *Add to repo* |

---

## 9. Contacts & Resources

- **Repository:** https://github.com/QuantumindSSI/lumena
- **Issue Tracker:** https://github.com/QuantumindSSI/lumena/issues
- **Discussions:** https://github.com/QuantumindSSI/lumena/discussions
- **Security:** https://github.com/QuantumindSSI/lumena/security/advisories
- **Matrix:** `#lumena:matrix.org`
- **License:** Apache 2.0

---

*Local-first memory for your agents. On your hardware. Your way.*
