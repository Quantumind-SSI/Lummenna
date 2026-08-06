<div align="center">

<pre>
        ┌───┐
     ┌──┤   ├──┐
     │  │ ✦ │  │
     └──┤   ├──┘
        └───┘
</pre>

<h1>Lumena</h1>

<p><strong>Local-first memory and context framework for sovereign AI agents.</strong></p>

<p>
  <a href="https://github.com/QuantumindSSI/lumen/actions"><img src="https://img.shields.io/github/actions/workflow/status/QuantumindSSI/lumen/ci.yml?branch=main&style=flat-square" alt="CI"></a>
  <a href="https://github.com/QuantumindSSI/lumen/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg?style=flat-square" alt="License"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/coverage-74%25-brightgreen?style=flat-square" alt="Coverage">
  <img src="https://img.shields.io/badge/tests-273%20passed-brightgreen?style=flat-square" alt="Tests">
</p>

<p><em>Beta software. API-stable; actively developed. Contributions welcome.</em></p>

</div>

---

## What is Lumena?

Lumena is a **local-first memory store for LLM agents** — it organizes agent memories in a structured memory palace (rooms, loci, chunks) with hybrid retrieval, managed decay, and native integrations. It runs entirely on your hardware with no cloud dependencies.

- **No cloud.** Embeddings run locally via ONNX Runtime. Storage is single-file SQLite.
- **Optional daemon.** Background scheduler auto-starts with `lumena serve`; can also run standalone with `lumena daemon start`.
- **Hybrid retrieval.** BM25 (SQLite FTS5), cosine-similarity vector search, and optional graph traversal with reciprocal rank fusion.
- **Managed memory lifecycle.** Three-layer forgetting: time-based decay, similarity interference, and budget eviction.
- **Integrations.** LangGraph checkpoint saver, LangChain memory adapter, MCP server, FastAPI REST API.

---

## Quick Start

```bash
# Clone and install
git clone https://github.com/QuantumindSSI/lumen.git
cd lumena
pip install -e ".[dev]"

# Initialize
lumena init --device generic

# Start the server
lumena serve
# Dashboard at http://localhost:8848/dashboard
# API docs at http://localhost:8848/docs
```

### Store and retrieve

```python
from lumen.config import LumenaConfig
from lumen.data.schema import get_connection
from lumen.force.mnemonic.store import store_memory

config = LumenaConfig()
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
from lumen.config import LumenaConfig
from lumen.data.schema import get_connection
from lumen.conversation import ConversationMemory

config = LumenaConfig()
conn = get_connection(config)
memory = ConversationMemory(config=config, conn=conn)

turn = memory.retrieve_and_assemble("What UI settings does the user like?")
print(turn.assembled_context)
```

```bash
$ lumena status
Lumena Status
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

Lumena is **beta software**. It works end-to-end with API versioning, comprehensive tests, and documented security limitations. It is suitable for evaluation, development, and trusted-LAN deployments.

| Dimension | Status | Detail |
|---|---|---|
| **Tests** | 273 passing, 9 skipped | 74% coverage. 41 test files. |
| **Storage** | Working | SQLite with WAL, FTS5, bi-temporal tracking, provenance chains. |
| **Retrieval** | Working | BM25 + dense + graph with RRF fusion. |
| **Forgetting** | Working | L1 decay (Ebbinghaus), L2 interference, L3 budget eviction. |
| **PII detection** | Working | Regex-based scanning at storage time. Configurable block/redact/hash. |
| **Audit logging** | Working | SQLite audit_log table with request tracing. |
| **API server** | Working | FastAPI with `/v1/` versioning, auth, rate limiting, CORS, security headers. |
| **MCP server** | Working | 7 tools (search, store, assemble, turn, feedback, status, dashboard). |
| **LangChain** | Working | LumenaChatMemory adapter (requires `langchain` package). |
| **LangGraph** | Working | LumenaCheckpointSaver (requires `langgraph` package). |
| **Encryption-at-rest** | Not implemented | Use OS-level disk encryption (LUKS, FileVault, BitLocker). Planned for v0.3.0. |
| **BEIR benchmarks** | Partially evaluated | 500-doc/20-query subset results available. Full-corpus evaluation deferred to HPC. |
| **P2P sharing** | Working | Beam protocol with AES-256-GCM encryption, HMAC-SHA256 signing, replay protection. Requires p2p key. |

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
| Performance (latency/footprint) | `python -m benchmarks.perf.run` | Harness ready; no results yet |
| BEIR subset evaluation | `python -m benchmarks.beir.run` | Run (500-doc/20-query subset results available) |
| All suites | `python -m benchmarks.run_all` | Wraps the above |

> **Note on results:** Retrieval benchmarks use a synthetic keyword-overlap corpus (1,000 passages, 50 queries) plus BEIR subset evaluation (500-passages, 20-query subsets across 5 standard datasets). The nDCG formula was fixed in a recent commit; re-run `python -m benchmarks.retrieval.run` to generate updated scores.

---

## Integrations

| Integration | What it does | How to use |
|---|---|---|
| **MCP Server** | Exposes Lumena tools to OpenCode, Claude Desktop | `python -m lumen.integrations.mcp_server` |
| **LangChain** | `LumenaChatMemory` adapter | `pip install langchain` |
| **LangGraph** | `LumenaCheckpointSaver` for graph state | `pip install langgraph` |
| **FastAPI** | REST API with auth/rate-limiting | `lumena serve` |
| **OpenCode** | Native skill for memory workflows | See `INTEGRATIONS.md` |

---

## Project Structure

```
lumena/
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
tests/                41 test files, 273 tests
benchmarks/           11 benchmark suites
```

---

## Contributing

We welcome contributions. The best way to start:

1. **Read [`CONTRIBUTING.md`](CONTRIBUTING.md)** — setup, branch naming, code standards.
2. **Pick a `good first issue`** from the [issues tracker](https://github.com/QuantumindSSI/lumen/issues).
3. **Run the tests:** `pytest tests/` (must pass with ≥50% coverage).
4. **Submit a PR** against `main`.

### High-impact areas to contribute

- **Run the full BEIR harness** — generate leaderboard-scale retrieval benchmark results.
- **Add encryption-at-rest** — SQLCipher integration is the highest-priority security feature.
- **Write tests** — several modules lack dedicated test files. Pick one and add coverage.

### Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest tests/                        # Run full suite
pytest tests/ --cov=lumena            # With coverage
ruff check lumena/ tests/             # Lint
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
| [`docs/Lumena_Agentic_Memory_Whitepaper_v2.md`](docs/Lumena_Agentic_Memory_Whitepaper_v2.md) | Technical white paper |

---

## Community

- [GitHub Discussions](https://github.com/QuantumindSSI/lumen/discussions)
- [Issue Tracker](https://github.com/QuantumindSSI/lumen/issues)
- [Security Advisories](https://github.com/QuantumindSSI/lumen/security/advisories)
- Matrix: `#lumena:matrix.org`

---

## License

Apache 2.0. See [`LICENSE`](LICENSE).

---

<div align="center">

<p><em>Local-first memory for your agents. On your hardware. Your way.</em></p>

</div>