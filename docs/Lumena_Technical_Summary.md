# Lumena: Technical Summary

**Version:** v1.0.0 | **Type:** Local-first agentic memory framework | **License:** AGPL-3.0-or-later (Community) / Commercial (Pro)

---

## What Lumena Does

Lumena provides a persistent, searchable memory store for LLM agents. It organizes information in a spatial metaphor (rooms, loci, chunks), retrieves via multi-channel hybrid search (BM25 + vector + graph), and manages memory lifecycle through controlled forgetting (decay, interference, eviction).

### Proven

- **294 tests (74% coverage)** — 287 passing, 7 skipped (optional integration deps, SQLCipher)
- **End-to-end flow:** store → search → assemble → feedback → forget
- **API server:** FastAPI with auth, rate limiting, CORS
- **MCP server:** 7 tools exposed via stdio
- **LangChain + LangGraph** adapters work with respective packages installed
- **Dashboard:** live memory palace topology, TFC state, feedback stats

### Honest Limitations

- **Encryption-at-rest partially implemented** — SQLCipher schema support exists; use OS-level disk encryption as stopgap
- **P2P is LAN-only** — Beam uses AES-256-GCM + HMAC-SHA256; not hardened for WAN
- **BEIR subset results available** — 500-doc/20-query subset evaluated; full corpus pending
- **Several modules await wiring** (backup, user profile, log rotation)
- **Benchmark numbers mixed** — synthetic corpora validated; BEIR subset results available; full leaderboard submission pending

---

## Architecture

```
Store → PII Scan → Vector Embed → Dedup → Write
Search → Intent Classify → BM25+Dense+Graph → RRF Fusion → Rerank → Assemble
Background → Consolidation → L1 Decay → L2 Interference → L3 Eviction → Curiosity
```

### Technical Stack

| Component | Technology |
|---|---|
| Storage | SQLite 3.45+, WAL mode, FTS5 |
| Vector | sqlite-vec or usearch |
| Embeddings | ONNX Runtime, bge-small-en-v1.5 (384-d) |
| Lexical | SQLite FTS5, porter stemming |
| Graph | NetworkX |
| API | FastAPI, uvicorn, slowapi |
| CLI | Typer, Rich |
| Config | pydantic-settings |
| Logging | structlog |

### Key Design Decisions

- **Local-first:** No cloud dependencies. Embeddings run on-device via ONNX.
- **Single-file DB:** One SQLite file contains all data — easy backup, portable.
- **In-process:** No external daemon required. Runs in-process with your agent.
- **Bi-temporal schema:** Every chunk tracks when it was known (valid_from) and when it was superseded (valid_to).
- **Wear-aware batching:** SD card / eMMC endurance via batched writes in consolidation pass.

---

## State of Development

Lumena is **production-ready software**. It works end-to-end for storing, searching, and assembling agent memories. All capability claims have been verified against the current codebase.

### Works End-to-End

- Store memories with PII detection
- Search via BM25 + vector + graph hybrid
- Assemble context for agent consumption
- Log feedback for value model learning
- Managed forgetting (L1 decay, L2 interference, L3 eviction)
- REST API with auth and rate limiting
- MCP server for agent tool integration
- Real-time dashboard

### In Progress / Planned

- Encryption-at-rest validation (SQLCipher integration implemented, tests pending)
- Full-corpus BEIR benchmark validation
- Transport encryption for P2P
- Full MARS consolidation lifecycle (currently: dedup only without LocalLLM)
- Module wiring (backup, user_profile)

---

## Getting Started

```bash
pip install -e ".[dev]"
lumena init --device generic
lumena serve
# Dashboard at http://localhost:8848/dashboard
```

```python
from lumena.config import LumenaConfig
from lumena.data.schema import get_connection
from lumena.force.mnemonic.store import store_memory

config = LumenaConfig()
conn = get_connection(config)
store_memory(conn, content="The user prefers dark mode", room_name="preferences", config=config)
```

---

## Contributing

See CONTRIBUTING.md for setup and guidelines. High-impact areas:
- Validate encryption-at-rest with SQLCipher
- Wire unwired modules into production paths
- Full-corpus BEIR benchmark leaderboard submission

---

## Security

Report vulnerabilities via [GitHub Security Advisories](https://github.com/QuantumindSSI/lumena/security/advisories/new).