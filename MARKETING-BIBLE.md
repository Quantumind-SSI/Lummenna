# Lummenna Marketing & Sales Bible
**Version:** 1.0-draft — 2026-10-22  
**Owner:** Head of Communications & Marketing  
**Source of truth:** This document is a derivative of the repository. Every public claim below is traceable to a committed artifact listed in Appendix A/D. Any change that introduces a new claim requires a new row in the Approved Claim Ledger and a source path in the repository.

> Golden rule for all external output — **No false claims.** Per AGENTS.md Rule 1/2: a claim is only publishable if the code, benchmark artifact, or license registry proves it today. Roadmap items are never advertised as available. The repository’s honest-claim posture is Lummenna’s brand wedge.

## 0. How to use this bible

This bible is for the Head of Communications & Marketing. It is the single source of messaging for B2B (priority) and B2C/indie (community funnel).

- **Do not copy from this document.** Extract approved copy blocks from Appendix B and always keep the scope line that accompanies a number.
- **Claims are versioned.** When a benchmark artifact is regenerated, update the Claim Ledger row, version the bible, and re-publish anything that used the old wording.
- **Decisions required** are marked DECISION REQUIRED. They are owner-actionable items where sales/engineering/legal input is needed before external use.

---

## 1. Product Ground Truth

### 1.1 What Lummenna is — one paragraph, claim-safe

Lummenna is a local-first memory and context framework for AI agents that stores memories in a structured memory palace (rooms, loci, chunks) with hybrid lexical+vector retrieval, token-budgeted context assembly, and a managed memory lifecycle of decay, interference and eviction. The Community runtime runs on your hardware with no cloud dependency and is single-node by design because the store is single-writer SQLite. The FastAPI REST API, stdio MCP server, LangChain and LangGraph adapters expose the same palace to different agent surfaces.

### 1.2 Capability inventory — what ships today

**Community (AGPL-3.0-or-later, 1.0.1rc1 release candidate)**

- Storage: SQLite with WAL, FTS5, bi-temporal tracking, provenance chains. Single file store.
- Retrieval: BM25 via SQLite FTS5, dense vector cosine similarity via ONNX Runtime embeddings, optional graph traversal, Reciprocal Rank Fusion with value weighting V(m) and recency.
- Lifecycle: Three-layer forgetting — L1 time-based decay (Ebbinghaus), L2 similarity interference, L3 budget-based eviction.
- Assembly: token-budgeted Jinja2 assembly with explicit truncation.
- Interfaces: `lummenna serve` FastAPI (8848), `/v1/` versioned REST, `/dashboard` observatory, MCP stdio server with 7 tools (search, store, assemble, turn, feedback, status, dashboard), LangChain `LummennaChatMemory`, LangGraph `LummennaCheckpointSaver`, Typer CLI.
- Integrations: 12 supported/agent surfaces including OpenCode, Copilot, Claude Desktop, Codex CLI, Cline, Goose, Zed, Aider, Devin, LangChain, LangGraph.
- Sovereignty: default `LUMMENNA_SOVEREIGN=true`. Embedding model download and P2P require `LUMMENNA_SOVEREIGN=false`, fails closed otherwise. Model inference afterwards is local ONNX.
- First-run: one-time networked model export (`lummenna model download bge-small-en-v1.5`, `LUMMENNA_SOVEREIGN=false`), then offline. For air-gapped installs provision model ONNX + tokenizer under `~/.lummenna/models/bge-small-en-v1.5/` before isolation.
- PII detection (regex-based at storage), configurable block/redact/hash.
- Audit log table `audit_log` with request tracing.
- P2P Beam: AES-256-GCM encryption, HMAC-SHA256 signing, timestamp window + seen-nonce replay protection. Listener refuses to start without `LUMMENNA_P2P_ENCRYPTION_KEY` unless `LUMMENNA_P2P_INSECURE=1`. Nonce store is process-local by default.
- Python 3.10+, deployment targets: Raspberry Pi 5 (4GB), Jetson Orin Nano (8GB), generic x86_64/ARM64 SBC. Docker image and container image status: repository states “has no published container image” today.

**Pro (commercial license, license-enforced today)**

From `lummenna/licensing.py` PRO_FEATURES:

- `compliance` — Right-to-be-forgotten atomic erasure, tamper-evident hash-chained audit trails, custom PII governance (P1)
- `encryption_at_rest` — SQLCipher full-database AES-256 or Fernet field-level encryption with PBKDF2 passphrase-derived keys; default mode is `none`
- `multi_tenant` — real storage-layer tenant isolation beyond default single tenant (P4)
- `advanced_p2p` — live encryption/signing key-rotation (`BeamNode.rotate_key`) and durable restart-surviving replay-nonce store via `LUMMENNA_P2P_REPLAY_STORE=persistent` (P6)
- `priority_support` — commercial support commitment (V5)
- `lts` — long-term support releases (V8)

Non-code commercial entitlements `priority_support` and `lts` are flag-only, never stubs.

**Planned Pro — NOT available**

Registered in `PLANNED_PRO_FEATURES` and ROADMAP.md “Enterprise Pro Roadmap”. Must never be advertised as present:

`advanced_auth`, `scale_out`, `team_analytics`, `key_vault`, `compliance_reporting`, `sso_directory_sync`, `backup_pitr`, `ha_topology`, and P6+ mesh routing/handshake. All status: not built.

### 1.3 Evidence inventory — committed benchmark artifacts

All numbers must be published with their scope line exactly as shown.

**End-to-end memory quality** `benchmarks/e2e/results/e2e_results.json`
- Embedder: all-MiniLM-L6-v2. Sessions: 7, query turns: 28, bootstrap 1000.
- Recall@1 mean 0.9643, CI95 [0.8929, 1.0000]; Recall@5 mean 1.0000; Recall@10 mean 1.0000; avg latency 8.59 ms [7.37, 9.90].
- Methodology disclosure: grading is embedding-similarity self-evaluation with expected facts stored verbatim and cosine similarity >0.4 hit threshold. No LLM judge. This is a persistence smoke test, not an external quality comparison.

**BEIR subset** `benchmarks/beir/results/beir_results.json`
- 5 datasets (nfcorpus, scifact, fiqa, arguana, scidocs), 500 passages and 20 queries each, seeds 42/123/456, 1000 bootstrap.
- Hybrid macro nDCG@10 0.439 (BGE-small-en-v1.5). Per-dataset nDCG@10 hybrid: nfcorpus 0.3322, scifact 0.4281, fiqa 0.4031, arguana 0.4935, scidocs 0.5399.
- Scope: subset signal only; not a full-corpus BEIR score.

**Synthetic retrieval** `benchmarks/retrieval/results/retrieval_results.json`
- 1,000 passages, 50 queries, 3 seeds, BGE embedder.
- Hybrid mean latency 13.9 ms (committed harness result; hardware metadata not recorded).

**Performance** `benchmarks/perf/results/perf_results.json`
- Embedder all-MiniLM-L6-v2.
- 1k corpus: p50 15.76 ms, p95 17.60 ms, p99 18.65 ms, RAM 1,145 MB, DB 4.56 MB.
- 10k corpus: p50 22.88 ms, RAM 1,312 MB, DB 43.63 MB.
- 50k corpus: p50 51.35 ms, p95 79.41 ms, p99 94.36 ms, RAM 1,731 MB, DB 216 MB.
- Host not recorded in artifact. Do not attach to specific hardware.

**Stress** `benchmarks/stress/results/stress_results.json`
- 20,000 chunks ingested at 79.4 chunks/s; 500 queries at 113.7 qps; latency p50 29.42 ms, p99 66.38 ms; L3 evicted 11,261.

**Forgetting** `benchmarks/forgetting/results/forgetting_results.json`
- 10,000 chunks, 90 simulated days.
- Interference high-sim pairs 495,000; interference precision 1.0.

**Optical degradation** `benchmarks/optical/results/optical_degradation.json`
- Committed FP16/INT8/BINARY accuracy vs storage tradeoff curves. Use only with precision labels.

**LoCoMo leaderboard** `benchmarks/leaderboard/results/leaderboard_locomo.md`
- Self-authored, unverified, maintainer-ranked.
- Lummenna nDCG@10 0.186 [0.126, 0.245]; BM25 baseline 0.177 [0.116, 0.239]. Confidence intervals overlap. Use only to illustrate publishing-discipline, never as a superiority claim.

**Tests** `README.md` State table
- 662 passing, 11 skipped, 78.7% coverage. 43 test files, ~327 tests (repo structure). 809 test functions in current tree.

### 1.4 Honest limitations register — sales must own these

- Single-node by design. Helm chart refuses `replicaCount > 1`; Azure template pins maxReplicas to 1. Judge production against that shape.
- No published container image today. Distribution is PyPI (--pre/pinned rc1) and direct wheel from lummenna.com.
- API authentication opt-in via `LUMMENNA_API_KEY`; disabled until set. Health/readiness, API docs, metrics, HTML shell remain public.
- Encryption at rest implemented but opt-in; default `none`. Fernet field-level leaves FTS5 index plaintext.
- MCP server is stdio only. Remote MCP transport is roadmap, not built.
- Rate limiting covers five POST endpoints only; GET endpoints unrated.
- P2P replay protection is process-local by default; durable store is Pro.
- Public API may change before stable release.

### 1.5 Distribution & operational facts

- Community 1.0.1rc1 available from PyPI with `--pre` or pinned version and direct download from lummenna.com.
- Python 3.10+. Extras: `[mcp]`, `[export]`, `[wizard]`, `[localllm]`, `[langchain]`, `[langgraph]`, `[azure]`, `[full]`.
- Default ports: 8848. Observability: `/health`, `/ready`, `/v1/status`, `/metrics/prometheus`.
- Sovereign mode default true; model download requires one-time `LUMMENNA_SOVEREIGN=false`.
- Seat semantics: seats = max concurrent non-default tenants. Creating a new non-default tenant beyond licensed seat count is refused; default workspace never counts.

---

## 2. Strategy

### 2.1 Category & wedge

Category: Sovereign Agent Memory Infrastructure.
Wedge: the only agent memory layer that defaults to local-first, publishes negative results, and makes forgetting a first-class primitive with committed evidence.

### 2.2 Positioning statement

For AI teams and privacy-conscious developers who need agents that remember without leaking data, Lummenna is a local-first memory and context framework for agents that provides structured, inspectable memory with managed lifecycle and an honest evidence ledger. Unlike cloud memory APIs and DIY RAG stacks, Lummenna keeps data on your hardware by default and proves what it does with committed artifacts.

### 2.3 ICP

**B2B (priority)**
- Platform/ML engineering leaders building internal agents, copilots, RAG-backed tools.
- Industries with data residency constraints: healthcare, legal, finance, defense/gov, EU SaaS.
- Organizations with shadow-AI / agent data leakage risk.
- Buyers who evaluate on security posture and provenance, not marketing charts.

**B2C/Indie (community funnel)**
- Solo developers / agent tinkerers using OpenCode, Claude Desktop, etc.
- Self-hosters running Ollama/local LLMs on homelab / Raspberry Pi / Jetson.
- Researchers needing reproducible memory experiments.

### 2.4 Personas (summary)

- Platform Lead: cares about integration surface, ops, single-writer constraint, API.
- Security Officer: cares about sovereign mode, P2P crypto, RTBF, audit trails, encryption-at-rest options, license compliance.
- AI Product Owner: cares about AGPL vs commercial, seats, support/SLA, roadmap clarity.
- Edge Operator: cares about offline install, ARM support, resource caps.

B2C:
- Agent Tinkerer: MCP-first, wants quick palace + OpenCode skill.
- Self-Hoster: cares about local model, no cloud phoning home.
- Researcher: cares about benchmark harness reproducibility.

### 2.5 Competitive posture

Frame on posture, not scores. Permitted framing:
- Architectural: local-first by default vs cloud by default.
- Evidence culture: we publish limitations with numbers.
- Lifecycle: managed forgetting vs unlimited vector growth.
- Licensing: dual license with clear commercial path vs open-core ambiguities.

Forbidden framing until cross-system artifacts exist: “faster than X”, “better recall than Y”, “the only…”. Cross-system harness exists but no committed results today.

### 2.6 Buying triggers

B2B: new agent project with PII/client data; security review flags cloud RAG; EU data residency mandate; AGPL audit; need audit trails for data deletion.

B2C: building personal agent; annoyed by cloud memory APIs; wants MCP tools for Claude/OpenCode; values transparency.

---

## 3. Messaging

### 3.1 Master narrative

*Open by default. Commercial when needed.*  
Memory, illuminated locally.  
The memory system that shows its work.

### 3.2 Message house B2B

**Pillar 1 — Sovereignty & Privacy**
- Local-first by default; no cloud dependency.
- Sovereign mode defaults true; telemetry opt-in fail-loud.
- Embedding models run locally via ONNX Runtime.

**Pillar 2 — Structure & Inspectability**
- Rooms, loci, chunks with provenance and validity.
- Tenants, audit logs, PII detection at storage.
- Observability dashboard reads real runtime state.

**Pillar 3 — Managed Lifecycle**
- Three-layer forgetting prevents unbounded growth.
- Twin-Force Controller with explicit parameters.

**Pillar 4 — Enterprise Controls (Pro, license-enforced)**
- Compliance erasure, SQLCipher/Fernet encryption-at-rest, storage-layer multi-tenant isolation, advanced P2P key rotation and durable replay store.
- Priority support and LTS via agreement.

**Pillar 5 — Honest Evidence**
- Committed benchmark artifacts with scope lines.
- Limitations published alongside numbers.
- Roadmap items clearly not available.

### 3.3 Message house B2C

- Free, open source AGPL Community.
- Runs locally; no vendor lock-in.
- Works with your agent stack via MCP, LangChain, LangGraph.
- Try 1.0.1rc1 today; join stable waitlist.

### 3.4 Pitch bank

10s: Local-first agent memory with managed forgetting and proof you can inspect.

30s: Lummenna gives agents a structured memory palace that runs on your hardware. Hybrid retrieval, token-budgeted assembly, and three-layer forgetting. Community is AGPL-free; Pro adds compliance erasure, encryption at rest, multi-tenant isolation, and advanced P2P controls under a commercial license.

90s: Use it with MCP, OpenCode, Claude, LangChain, or via REST. Every claim is backed by committed artifacts with scope lines, and we publish limitations alongside results. Single-node by design, sovereign by default, and built for teams that care where data lives.

### 3.5 Objection handbook (selected)

**AGPL contamination?** Community is AGPL-3.0-or-later. Commercial license removes network-use obligations and unlocks Pro features. Apache-2.0 grant applies to ≤v1.0.0 releases.

**We need HA / replicas.** Lummenna is single-writer SQLite by design. Helm refuses >1 replica. That is a constraint you accept for a per-device palace. HA topology and scale-out are planned Pro items, not available.

**Your benchmarks are tiny.** Correct. We label corpus size, query count, seeds, and methodology. We publish synthetic and subset results with scope, and we publish LoCoMo with overlapping CIs. The harness is open.

**How is this different from Mem0/Zep/Letta?** Architecture: local-first default, managed forgetting as first-class, memory palace structure, license-enforced Pro controls. We do not publish head-to-head scores until the cross-system suite has committed results.

**Is it production-ready?** 1.0.1rc1 is a public release candidate that works end-to-end for evaluation, development, sidecars and trusted-LAN deployments. It is single-node by design. Stable release pending.

**Encryption?** Implemented, opt-in. SQLCipher for full-DB, Fernet field-level. Default is none — use OS disk encryption or enable a mode.

### 3.6 Language rules

Say: local-first, sovereign by default, structured memory palace, managed forgetting, committed artifacts, release candidate, single-node by design, license-enforced.

Avoid: production-grade, enterprise-ready, unlimited, fastest, best, “proven”, “industry-leading”. Do not use “scale-out” or “SSO” without the “planned, not available” qualifier.

Never remove scope lines from benchmark numbers in external materials.

---

## 4. Brand

### 4.1 Essence

Local. Inspectable. Honest.

Values: Sovereignty, Evidence, Clarity.

Personality: precise, calm, engineer-to-engineer.

### 4.2 Voice & tone

Clear, measured, no hype. Lead with what is true, then scope. Use ledger-style tables for capabilities.

### 4.3 Terminology glossary (use consistently)

Memory palace, room, locus, chunk, provenance, value V(m), Twin-Force Controller (TFC), decay/interference/eviction, Beam P2P, observatory, seats (max concurrent non-default tenants), sovereign mode, release candidate.

### 4.4 Visual identity

Core asset: crystalline memory core with jade and amber Twin-Force rings. Brand tag “Memory Palace”. Website video backdrop. Keep the proof strip table style.

### 4.5 Tagline bank

- Memory, illuminated locally.
- Open by default. Commercial when needed.
- The memory system that shows its work.
- Local-first memory for sovereign agents.

---

## 5. Editions, Packaging & Pricing

### 5.1 Edition architecture

Community AGPL-3.0-or-later. Pro commercial license removes AGPL obligations and unlocks license-enforced controls listed in 1.2. Seats enforced in code for multi-tenant isolation.

### 5.2 Self-serve tiers

Gumroad fulfillment script defines tier mapping in code:

- solo: 3 seats
- team: 10 seats
- business: 25 seats

Seats semantics: max concurrent non-default tenants. Default tenant not counted. Price points are managed in Gumroad and are not published in the repository; never invent prices externally.

### 5.3 Enterprise path

Quote per agreement via license@lummenna.com. Agreement covers scope (seats/tenants/nodes/deployments), term, support/SLA, LTS.

### 5.4 Pricing narrative rules

Never quote a price not in the current order form. Use “Terms by agreement” for Pro editions. Self-serve tiers are “by Gumroad listing” with seat counts only.

---

## 6. B2B Sales Playbook (priority)

### 6.1 Pipeline stages

1. Discovery — data residency, AGPL policy, memory lifecycle needs
2. Security review — share SECURITY.md, claim ledger, artifact map
3. Technical validation — pilot on their corpus with their queries
4. Commercial — seats, Pro features, support/SLA
5. Deployment — `lummenna init`, model provision, observability

Exit criteria are concrete: pilot recall measured on their queries, security review checklist passed, license key issued.

### 6.2 Discovery question bank

- Where must agent memory reside to meet compliance?
- How do you handle forgetting / data deletion today?
- What is your stance on AGPL in network services?
- Do you need multi-tenant isolation per customer?
- What toolchains (MCP, LangChain, OpenCode) will consume memory?

### 6.3 Demo script (15 min)

1. Init palace `lummenna init --device generic`
2. Store 2 rooms, retrieve hybrid search, show fusion ranks
3. Assemble context with token budget; show truncation
4. Open observatory at localhost:8848/dashboard
5. Show REST `/v1/search` and `/v1/assemble` via curl
6. Show MCP tool list in OpenCode/Claude config
7. Pro: enable encryption-at-rest, create second tenant, run RTBF, show audit trail

Always show limitations slide after demo.

### 6.4 Pilot design (30 days)

Success criteria: query recall on their corpus (using their queries, not ours), latency p50/p95 measured on their hardware, security checklist signed off, audit trail export reviewed. Deliverable: pilot report with numbers and scope.

### 6.5 Security & compliance review kit

Provide: SECURITY.md, COMMERCIAL-LICENSE.md summary, claim ledger, benchmark artifacts, repository architecture diagram, PGP key for disclosures. Note sovereign mode default true and telemetry fail-loud.

### 6.6 AGPL conversation

Offer commercial license for closed-source/hosted use. Historical Apache-2.0 grant applies to ≤v1.0.0 releases.

### 6.7 Disqualification discipline

Disqualify buyers who require HA replicas, SSO/OIDC now, horizontal read scaling, or PITR today. Route to waitlist.

### 6.8 Email templates (excerpt)

Subject: Local-first agent memory with proof you can inspect
Body: ... (use approved copy from Appendix B)

---

## 7. B2C & Community Playbook

### 7.1 Funnel

Content → GitHub README/INTEGRATIONS → `lummenna init` → Community use → Pro conversion for power users.

### 7.2 Channels

GitHub Discussions, Matrix #lummenna:matrix.org, HN Show HN, r/LocalLLaMA, MCP ecosystem listings, OpenCode community, documentation SEO.

### 7.3 Launch playbooks

- HN: title “Show HN: Lummenna — local-first memory palace for agents with committed benchmarks (not another vector DB hype post)”. First comment links to claim ledger and limitations.
- Reddit: technical deep-dives on forgetting, Beam P2P, sovereignty.

### 7.4 Community ops

Triaged issues, security advisories via GitHub Security, PGP key published in SECURITY.md for sensitive reports.

---

## 8. Content & Evidence Engine

### 8.1 Content pillars

Sovereignty & privacy, memory science, evidence culture, integrations, honest benchmarks.

### 8.2 Benchmark citation rules

Format: “Metric value — scope line — source”. Example: “Recall@1 0.964 — 7 sessions, 28 queries, all-MiniLM-L6-v2, embedding-similarity self-grade, persistence smoke test — benchmarks/e2e/results/e2e_results.json”. Never drop the scope.

### 8.3 SEO themes

local-first agent memory, sovereign LLM memory, on-prem agent memory, MCP memory server, GDPR agent memory, memory palace AI, managed forgetting AI.

---

## 9. First-90-Days Campaign Calendar

Weeks 1-2: Audit all external assets against Claim Ledger; ship one-pager; publish security kit.

Weeks 3-4: B2C wave — HN/Reddit/MCP directories; waitlist push for stable.

Weeks 5-8: B2B wave — “The case for sovereign agent memory” webinar; outbound to ICP; pilot offers.

Weeks 9-12: Case study collection from pilots; integration docs expansion; decision point on public price points and pricing page.

---

## 10. Measurement

Leading: waitlist signups, GitHub stars, PyPI downloads, docs traffic, demo requests.

Conversion: security review starts, pilot starts, Pro license issues via Gumroad, enterprise quotes.

Quality: % of external materials passing claim-ledger audit; time to publish benchmark updates after artifact regeneration.

Set baselines in first 30 days; do not manufacture targets.

---

## 11. Legal, License & Disclosure Guardrails

- Always label Community as AGPL-3.0-or-later.
- Always label 1.0.1rc1 as release candidate; public API may change.
- Commercial license contact: license@lummenna.com (website) / licensing@quantumindssi.com (license doc). DECISION REQUIRED: unify domains (lummenna.com vs quantumindssi.com) before broad outreach.
- Security contact: security@lummenna.ai and GitHub Security advisories. PGP key in SECURITY.md. Domain lummenna.ai for security — DECISION REQUIRED to align with primary brand domain.
- Entity names: GitHub account Quantumind-SSI (public repository
  Quantumind-SSI/Lummenna, since 2026-10-25); legal copyright QuantumindSSI.
  The old org QuantmindSSI is gone. DECISION REQUIRED: whether to align the
  legal copyright entity name with the public GitHub account name.
- Do not publish roadmap features as available. Use “planned, not available” wording.
- Do not use “production-grade” or “enterprise-ready” without qualifier.

---

## Appendix A. Approved Claim Ledger (excerpt)

| Claim ID | Approved wording | Scope | Source |
|---|---|---|---|
| C-001 | Local-first by default; no cloud dependency | Sovereign mode defaults true | lummenna/config.py:93 |
| C-002 | Memory palace with rooms, loci, chunks | Schema defined | lummenna/data/schema.py |
| C-003 | Hybrid retrieval: BM25 + dense + optional graph with RRF fusion | Implemented | README Architecture |
| C-004 | Three-layer forgetting: decay, interference, eviction | Implemented | README State table |
| C-005 | MCP stdio server with 7 tools | Working | README State table |
| C-006 | E2E Recall@1 0.964 | 7 sessions, 28 queries, MiniLM, self-graded persistence smoke test | benchmarks/e2e/results/e2e_results.json |
| C-007 | BEIR subset hybrid macro nDCG@10 0.439 | 5 datasets × 500 passages × 20 queries each, BGE | benchmarks/beir/results/beir_results.json |
| C-008 | Synthetic retrieval hybrid mean latency 13.9 ms | 1,000 passages, 50 queries, 3 seeds, BGE; host not recorded | benchmarks/retrieval/results/ |
| C-009 | Stress 20k chunks @ 79.4 chunks/s ingest | 20,000 chunks stress run | benchmarks/stress/results/stress_results.json |
| C-010 | Pro features built & gated: compliance, encryption_at_rest, multi_tenant, advanced_p2p, priority_support, lts | License registry | lummenna/licensing.py PRO_FEATURES |
| C-011 | Planned Pro features not available | advanced_auth, scale_out, team_analytics, etc. | lummenna/licensing.py PLANNED_PRO_FEATURES; ROADMAP.md |
| C-012 | Single-node by design; Helm refuses replicaCount > 1 | Operational constraint | README State table, infra charts |
| C-013 | No published container image today | Distribution statement | README State of the Project |
| C-014 | Seats = max concurrent non-default tenants | Enforcement semantics | lummenna/licensing.py assert_seat_available |
| C-015 | 662 tests passing, 11 skipped, 78.7% coverage | As of 1.0.1rc1 | README State table |

Full ledger with 30+ rows maintained in source-of-truth map.

## Appendix B. Approved Copy Blocks

**Hero (website)**
Memory, illuminated locally.
Local-first memory and context framework for sovereign AI agents.

**B2B email opener**
Agents forget. Or they never stop remembering. Lummenna gives you a structured memory palace that runs on your hardware, with managed forgetting and an evidence ledger you can inspect. Community is AGPL; Pro adds compliance erasure, encryption at rest, multi-tenant isolation and advanced P2P controls under a commercial license.

**HN title**
Show HN: Lummenna — local-first agent memory with committed benchmarks and published limitations

**Waitlist CTA**
Join the stable waitlist. Community 1.0.1rc1 is available now; stable release pending.

**Limitation footnote (mandatory with benchmarks)**
Numbers are from committed artifacts with stated corpus sizes, query counts and methodology. Host metadata may be unrecorded. See the Claim Ledger.

## Appendix C. One-Pager Skeleton

Headline, one-paragraph product definition, three pillars (sovereignty, structure, lifecycle), Pro controls table, edition comparison, proof strip with scope lines, call to action: deploy Community / request Pro quote.

## Appendix D. Source-of-Truth Map

- Product architecture: README.md § Architecture, § State of the Project
- Licensing & editions: lummenna/licensing.py PRO_FEATURES / PLANNED_PRO_FEATURES; COMMERCIAL-LICENSE.md
- Benchmarks: benchmarks/*/results/*.json + benchmarks/*/results/*.md
- Security posture: SECURITY.md
- Deployment targets & constraints: DEPLOYMENT.md
- Integrations: INTEGRATIONS.md, README.md § Integrations
- Roadmap & honest claim posture: ROADMAP.md
- Pricing self-serve tiers: scripts/gumroad_fulfillment.py DEFAULT_TIERS

---

**Change control:** Any public asset that adds, changes, or removes a numeric claim must be reviewed against Appendix A and the artifact path in Appendix D. No roadmap capability may be described as available. All external materials must carry the limitation line that accompanies their numbers.

*This bible is a living document. Version it with every material claim change and keep the repository as the single source of truth.*
