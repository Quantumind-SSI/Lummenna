<div align="center">

<pre>
        ┌───┐
     ┌──┤   ├──┐
     │  │ ✦ │  │
     └──┤   ├──┘
        └───┘
</pre>

<h1>Lummenna</h1>

<p><strong>Local-first memory and context framework for sovereign AI agents.</strong></p>

<p>
  <a href="https://github.com/QuantmindSSI/lummenna/actions"><img src="https://img.shields.io/github/actions/workflow/status/QuantumindSSI/lummenna/ci.yml?branch=main&style=flat-square" alt="CI"></a>
  <a href="https://github.com/QuantmindSSI/lummenna/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-AGPL--3.0%20%7C%20Commercial-blue.svg?style=flat-square" alt="License"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/coverage-75%25-brightgreen?style=flat-square" alt="Coverage">
  <img src="https://img.shields.io/badge/tests-320%20passed-brightgreen?style=flat-square" alt="Tests">
</p>

<p><em>Production-ready. API-stable; actively developed. Contributions welcome.</em></p>

</div>

---

## What is Lummenna?

Lummenna is a **local-first memory store for LLM agents** — it organizes agent memories in a structured memory palace (rooms, loci, chunks) with hybrid retrieval, managed decay, and native integrations. It runs entirely on your hardware with no cloud dependencies.

- **No cloud.** Embeddings run locally via ONNX Runtime. Storage is single-file SQLite.
- **Optional daemon.** Background scheduler auto-starts with `lummenna serve`; can also run standalone with `lummenna daemon start`.
- **Hybrid retrieval.** BM25 (SQLite FTS5), cosine-similarity vector search, and optional graph traversal with reciprocal rank fusion.
- **Managed memory lifecycle.** Three-layer forgetting: time-based decay, similarity interference, and budget eviction.
- **Integrations.** LangGraph checkpoint saver, LangChain memory adapter, MCP server, FastAPI REST API.

---

## Quick Start

```bash
# Clone and install (lean runtime — no torch/CUDA)
git clone https://github.com/QuantmindSSI/lummenna.git
cd lummenna
pip install -e .            # runtime: sqlite-vec, onnxruntime, transformers tokenizer…

# Initialize
lummenna init --device generic

# Start the server
lummenna serve
# Dashboard at http://localhost:8848/dashboard
# API docs at http://localhost:8848/docs
```

> **First run needs an embedding model.** By default `lummenna` will try to export
> one, which requires the heavy `[export]` toolchain. The lean, recommended path
> is a **prebuilt model bundle** (no toolchain): set `LUMMENNA_PREBUILT_MODEL_URL`
> or use the one-command installer.

### Install options (extras)

The base install is deliberately **lean** (no torch/CUDA). Add extras only when needed:

| Install | Adds | When |
|---|---|---|
| `pip install lummenna` | Core runtime + inference | Always |
| `pip install 'lummenna[mcp]'` | MCP server for coding agents | Using OpenCode/Copilot/Claude/etc. |
| `pip install 'lummenna[export]'` | ONNX export toolchain (optimum → torch, ~2GB) | Only to *build* a model yourself |
| `pip install 'lummenna[wizard]'` | spaCy onboarding wizard | `lummenna illuminate` |
| `pip install 'lummenna[localllm]'` | On-device LLM (llama-cpp) | Narrative consolidation |
| `pip install 'lummenna[langchain]'` / `[langgraph]` | Framework adapters | Those frameworks |
| `pip install 'lummenna[full]'` | Everything above | Kitchen-sink local dev |

### Store and retrieve

```python
from lummenna.config import LummennaConfig
from lummenna.data.schema import get_connection
from lummenna.force.mnemonic.store import store_memory

config = LummennaConfig()
conn = get_connection(config)

chunk_id = store_memory(
    conn,
    content="User prefers dark mode and large fonts",
    room_name="preferences",
    config=config,
)
conn.close()
```

```python
from lummenna.config import LummennaConfig
from lummenna.data.schema import get_connection
from lummenna.conversation import ConversationMemory

config = LummennaConfig()
conn = get_connection(config)
memory = ConversationMemory(config=config, conn=conn)

turn = memory.retrieve_and_assemble("What UI settings does the user like?")
print(turn.assembled_context)
```

```bash
$ lummenna status
Lummenna Status
Device: generic
Rooms: 5
Active chunks: 58
Context budget: 2048 tokens
TFC → e=0.50 a=0.50 tau=7.0 r=3
```

### API endpoints

```
GET  /health            Liveness probe (unversioned)
GET  /dashboard         Effectiveness dashboard (HTML)
GET  /metrics           Machine-readable metrics
GET  /v1/status        Palace overview
POST /v1/search       Semantic + lexical hybrid search
POST /v1/store        Store a memory chunk
POST /v1/feedback     Log explicit or implicit feedback
POST /v1/assemble     Retrieve + assemble context in one call
POST /v1/turn         Store full conversation turn
GET  /v1/dashboard-data Dashboard data as JSON
```

---

## Architecture

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

---

## State of the Project

Lummenna is **production-ready software**. It works end-to-end with API versioning, comprehensive tests, and documented security limitations. It is suitable for production, evaluation, development, and trusted-LAN deployments.

| Dimension | Status | Detail |
|---|---|---|
| **Tests** | 320 passing, 7 skipped | 75% coverage. 43 test files. |
| **Storage** | Working | SQLite with WAL, FTS5, bi-temporal tracking, provenance chains. |
| **Retrieval** | Working | BM25 + dense + graph with RRF fusion. |
| **Forgetting** | Working | L1 decay (Ebbinghaus), L2 interference, L3 budget eviction. |
| **PII detection** | Working | Regex-based scanning at storage time. Configurable block/redact/hash. |
| **Audit logging** | Working | SQLite audit_log table with request tracing. |
| **API server** | Working | FastAPI with `/v1/` versioning, opt-in API-key auth (off until `LUMMENNA_API_KEY` is set), rate limiting on POST endpoints, CORS, security headers. |
| **MCP server** | Working | 7 tools (search, store, assemble, turn, feedback, status, dashboard). |
| **LangChain** | Working | LummennaChatMemory adapter (requires `langchain` package). |
| **LangGraph** | Working | LummennaCheckpointSaver (requires `langgraph` package). |
| **Encryption-at-rest** | Implemented, opt-in | SQLCipher (full-DB) or Fernet (field-level) via `LUMMENNA_DATABASE_ENCRYPTION_MODE`. Default is `none` — enable it or use OS-level disk encryption. |
| **BEIR benchmarks** | Partially evaluated | 500-doc/20-query subset results available. Full-corpus evaluation deferred to HPC. |
| **P2P sharing** | Working | Beam protocol with AES-256-GCM encryption, HMAC-SHA256 signing, replay protection. Requires p2p key. Replay nonces are process-local by default; durable (restart-surviving) nonces are Pro via `LUMMENNA_P2P_REPLAY_STORE=persistent`. |

---

## Benchmark Suites

All run with a single command from the repo root:

| Suite | Command | Status |
|---|---|---|
| Retrieval (R@k, nDCG, MRR) | `python -m benchmarks.retrieval.run` | Run (synthetic corpus) |
| E2E memory quality | `python -m benchmarks.e2e.run` | Run (28 queries) |
| Navigation efficiency | `python -m benchmarks.navigation.run` | Run |
| Ablation (component isolation) | `python -m benchmarks.ablation.run` | Run |
| Forgetting (90-day survival) | `python -m benchmarks.forgetting.run` | Run (results available) |
| Performance (latency/footprint) | `python -m benchmarks.perf.run` | Run (results available; x86_64) |
| BEIR subset evaluation | `python -m benchmarks.beir.run` | Run (500-doc/20-query subset results available) |
| Optical degradation | `python -m benchmarks.optical.run` | Run (results available) |
| TFC sensitivity | `python -m benchmarks.tfc.run` | Run (results available) |
| Stress (bulk ingest) | `python -m benchmarks.stress.run` | Run (20k-chunk results available; x86_64) |
| Cross-system (vs Chroma/FAISS) | `python -m benchmarks.cross_system.run` | Harness ready; no results yet |
| All suites | `python -m benchmarks.run_all` | Wraps all 11 suites |

> **Note on results:** Retrieval benchmarks use a synthetic keyword-overlap corpus (1,000 passages, 50 queries) plus BEIR subset evaluation (500-passage, 20-query subsets across 5 standard datasets). The synthetic corpus is deliberately easy (BM25 near-saturates nDCG), so treat those numbers as harness sanity checks, not retrieval-quality claims — the BEIR subsets are the meaningful signal. The committed retrieval artifact was regenerated with real embedders (all-MiniLM-L6-v2 and BAAI/bge-small-en-v1.5); benchmarks refuse to run with mock embeddings.

---

## Integrations

| Integration | What it does | How to use |
|---|---|---|
| **MCP Server** | Exposes Lummenna tools to OpenCode, Claude Desktop | `python -m lummenna.integrations.mcp_server` |
| **LangChain** | `LummennaChatMemory` adapter | `pip install langchain` |
| **LangGraph** | `LummennaCheckpointSaver` for graph state | `pip install langgraph` |
| **FastAPI** | REST API with auth/rate-limiting | `lummenna serve` |
| **OpenCode** | Native skill for memory workflows | See `INTEGRATIONS.md` |

---

## Project Structure

```
lummenna/
├── config.py          Configuration (pydantic-settings)
├── search.py          Search pipeline orchestration
├── fusion.py          RRF fusion + reranking
├── controller.py      Twin-Force state controller
├── conversation.py    Context assembly + turn tracking
├── repair.py          Self-healing retrieval
├── intent.py          Intent router (keyword + optional LR)
├── api/               FastAPI server + dashboard
├── cli/               Typer CLI
├── data/              Schema, migrations, backup
├── force/
│   ├── mnemonic/      Store, retrieval, decay, interference, eviction, provenance
│   └── contextual/    Embedding, token budget, assembly
├── integrations/      LangChain, LangGraph, MCP server
├── p2p/               Beam P2P sharing protocol
├── sovereign/         FRQAD, optical quantization, local LLM
├── brand/             Error hierarchy
└── compliance/        Safety forgetting, PII audit
tests/                43 test files, 327 tests
benchmarks/           11 benchmark suites
```

---

## Contributing

We welcome contributions. The best way to start:

1. **Read [`CONTRIBUTING.md`](CONTRIBUTING.md)** — setup, branch naming, code standards.
2. **Pick a `good first issue`** from the [issues tracker](https://github.com/QuantmindSSI/lummenna/issues).
3. **Run the tests:** `pytest tests/` (must pass with ≥50% coverage).
4. **Submit a PR** against `main`.

### High-impact areas to contribute

- **Run the full BEIR harness** — generate leaderboard-scale retrieval benchmark results.
- **Run the perf suite on real hardware** — RAM/latency footprint claims need measured artifacts (RPi5, Jetson, x86_64).
- **Write tests** — several modules lack dedicated test files. Pick one and add coverage.

### Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/                        # Run full suite
pytest tests/ --cov=lummenna            # With coverage
ruff check lummenna/ tests/             # Lint
```

---

## Documentation

| Document | Purpose |
|---|---|
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Production deployment guide |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to contribute |
| [`SECURITY.md`](SECURITY.md) | Security policy and known limitations |
| [`INTEGRATIONS.md`](INTEGRATIONS.md) | Integration guides for each platform |
| [`ROADMAP.md`](ROADMAP.md) | Development milestones and open work |
| [`docs/Lummenna_Whitepaper.md`](docs/Lummenna_Whitepaper.md) | Introductory white paper |

---

## Community

- [GitHub Discussions](https://github.com/QuantmindSSI/lummenna/discussions)
- [Issue Tracker](https://github.com/QuantmindSSI/lummenna/issues)
- [Security Advisories](https://github.com/QuantmindSSI/lummenna/security/advisories)
- Matrix: `#lummenna:matrix.org`

---

## License

Lummenna is **dual-licensed**:

- **Community Edition** — [AGPL-3.0-or-later](LICENSE). Free and open source.
  If you run a modified Lummenna as a network service, AGPL requires you to make
  your source available to its users.
- **Pro / Commercial Edition** — a commercial license from QuantumindSSI that
  removes the AGPL obligations and unlocks Pro features. See
  [`COMMERCIAL-LICENSE.md`](COMMERCIAL-LICENSE.md).

Versions up to and including v1.0.0 were released under Apache 2.0
([`LICENSES/Apache-2.0.txt`](LICENSES/Apache-2.0.txt)); that grant on those
releases is irrevocable. Commercial inquiries: **licensing@quantumindssi.com**.

---

<div align="center">

<p><em>Local-first memory for your agents. On your hardware. Your way.</em></p>

</div>