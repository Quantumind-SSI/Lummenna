# Lummenna Production Readiness Roadmap

This document tracks ongoing and future work for Lummenna.

> **Release status: Community preview prepared.** Version `1.0.1rc1` is built as
> an audited wheel and corresponding source archive for PyPI and direct
> distribution from lummenna.com. It has no published container image. The
> artifacts become public when Trusted Publishing and the website deployment
> complete. Section 3.3 tracks the remaining stable-release work.

---

## Phase 1 — Foundation (P0 Blockers) — CLOSED

### 1.1 Documentation Audit & Alignment
- [x] **P1.1 Re-access reinforcement** — Code landed in `search.py`. Docs incorrectly listed as "Open". Update README + whitepaper.
- [x] **P1.2 Intent router (LR classifier)** — Code landed in `intent.py`. Docs incorrectly listed as "Open". Update README + whitepaper.
- [x] **API authentication** — `X-API-Key` middleware verified in `api/server.py`. Documented in DEPLOYMENT.md §5.1.
- [x] **Encryption-at-rest inconsistency** — Fixed. Config fields removed from code, all docs aligned to state "not yet implemented, use OS-level encryption."

### 1.2 SQLite Encryption-at-Rest
- [ ] Implement SQLCipher integration (`PRAGMA key = ...`) in `get_connection()`
- [ ] Support Fernet field-level encryption fallback for standard sqlite3
- [x] OS-level encryption guidance (FileVault, BitLocker, LUKS) documented in DEPLOYMENT.md and SECURITY.md
- [x] File permissions enforcement: `~/.lummenna` is `700` by default

### 1.3 API Hardening
- [x] Document `X-API-Key` and `X-Tenant-ID` headers in API docs
- [x] Add rate-limiting verification tests
- [x] Add request-size limit tests
- [x] Security headers (HSTS, CSP for dashboard)

---

## Phase 2 — Quality & Scale Validation — CLOSED

### 2.1 Retrieval Benchmarks at Scale
- [ ] Run full BEIR subset evaluation (not sampled 3,000 docs) — deferred to HPC
- [ ] Submit or compare against MTEB leaderboard numbers
- [x] Publish benchmark results in `benchmarks/*/results/`

### 2.2 Optical Degradation Benchmarks
- [x] Benchmark R@k / nDCG at FP16 precision level
- [x] Benchmark R@k / nDCG at INT8 precision level
- [x] Benchmark R@k / nDCG at BINARY precision level
- [x] Document accuracy vs. storage trade-off curves

### 2.3 TFC Sensitivity Analysis
- [x] Grid search across TFC parameters
- [x] Justify default `e=0.50`, `a=0.50`, `τ=7.0`, `r=3`
- [x] Publish sensitivity report to `benchmarks/tfc/results/tfc_sensitivity.json`

### 2.4 V(m) Weight Calibration
- [ ] Run Nelder-Mead or gradient-based weight learning with ≥100 feedback samples
- [ ] Validate against human relevance judgments

---

## Phase 3 — Stress & Security Hardening — CLOSED

### 3.1 Load & Concurrency Stress Test
- [ ] 100k+ chunk ingestion test — harness exists (`benchmarks/stress/`); no committed result artifact. Earlier ad-hoc runs (~285 chunks/s, ~15 qps, peak 178 MB RSS) are unreproducible from the repo and must be re-run and committed before being cited.
- [ ] Multi-threaded query throughput test — same status as above
- [ ] Verify L3 budget eviction triggers correctly under RSS pressure — re-run with committed artifact

### 3.2 P2P / Beam Security Audit
- [x] Transport encryption — AES-256-GCM + HMAC-SHA256 with replay protection shipped in `lummenna/p2p/beam.py` / `lummenna/security/crypto.py`; a Beam listener refuses to start without a key
- [x] Harden Beam protocol for adversarial networks — key rotation (`BeamNode.rotate_key`) and the durable replay-nonce store (`lummenna/p2p/replay.py`, `LUMMENNA_P2P_REPLAY_STORE=persistent`) are shipped and Pro-gated under `advanced_p2p`. Remaining hardening (mesh routing, key-rotation handshake) is tracked under EP-P6+ below.
- [x] Document trust model and household-local limitation

### 3.3 Release Engineering
- [x] PGP key for vulnerability reporting — generated and published in SECURITY.md
- [x] Build an audited `1.0.1rc1` Community wheel and corresponding source
      archive with checksums and no private repository or incident material.
- [ ] Deploy the direct-download Community artifacts at lummenna.com.
- [ ] CI/CD: add `pip-audit`, fuzz tests, backend matrix tests
- [ ] Tag the first stable release after the `1.0.1rc1` Community preview. `release.yml` is wired and
      triggers on `v*.*.*`, but no tag has ever been pushed (`git tag -l` is
      empty), so the release workflow has never run.
- [ ] Publish the source distribution and wheel to PyPI. The `publish-pypi` job
      exists in `release.yml`; `pip install lummenna` fails until it runs.
- [ ] Publish the container image to GHCR. `docker-publish.yml` targets
      `ghcr.io/${{ github.repository_owner }}/lummenna`; no image is currently
      pullable.
- [ ] Publish release notes with closed/open gaps

---

## Phase 4: Remote MCP Transport (OPEN, Community)

### 4.1 Streamable HTTP transport for the MCP server

**Not built today.** `lummenna/integrations/mcp_server.py` calls
`mcp.run(transport="stdio")` and nothing else. A client must be able to spawn
the process, so Lummenna's MCP surface is unavailable to remote hosts,
containers, and browser-based clients. The documented workaround is the REST
API (`INTEGRATIONS.md`, "Option B"), which is a different protocol with a
different tool surface, not MCP.

Scope note: the target is **Streamable HTTP**, the transport introduced in MCP
revision 2025-03-26. The MCP specification defines exactly two standard
transports, stdio and Streamable HTTP; the standalone HTTP+SSE transport from
revision 2024-11-05 is deprecated, and SSE now appears only as the response
streaming mechanism *inside* Streamable HTTP. The pinned SDK still exposes
`transport="sse"` and `FastMCP.sse_app()` for backward compatibility. Shipping
that legacy transport is explicitly **out of scope**: it would be a new public
surface born deprecated.

This is Community work, not Pro. The stdio MCP server and the HTTP REST API are
both Community, so gating the transport that joins them would be arbitrary. It
is therefore deliberately **absent** from `PLANNED_PRO_FEATURES`.

Acceptance criteria:

- [ ] `lummenna mcp --transport streamable-http` serves the identical tool and
      resource surface as stdio, verified by a test asserting both transports
      report the same tool names, input schemas, and resource URIs
- [ ] **Per-session state.** `_state()` currently falls back to the module-level
      `_module_state` singleton, which is correct for stdio (one client, one
      process) and wrong for concurrent HTTP sessions. State must be resolved
      per request from the lifespan context, with a test proving two concurrent
      sessions cannot observe each other's request state
- [ ] **Concurrency against single-writer SQLite.** The palace is one SQLite
      database and `lummenna serve` may already hold it. Concurrent tool calls
      must serialise through the existing advisory locks (`lummenna/data/locks.py`)
      with a bounded timeout and a clear error, never a silent `database is
      locked` failure. Covered by a test that runs writes from concurrent
      sessions while a second process holds the lock
- [ ] **Authentication on by default.** stdio inherits the trust boundary of the
      spawning process; HTTP does not. The endpoint must refuse to start on a
      non-loopback interface without an auth credential configured, mirroring
      the REST API's `X-API-Key` check (`lummenna/api/server.py`). Tests prove
      unauthenticated requests are rejected and that startup fails closed
- [ ] **Origin validation and loopback default.** Bind `127.0.0.1` unless
      explicitly overridden, and validate the `Origin` header to prevent DNS
      rebinding attacks against a locally bound server, as the MCP transport
      security guidance requires. Tests cover a forged `Origin` and a
      non-loopback bind attempt
- [ ] **Sovereign posture preserved.** Exposing the server on a network
      interface is opt-in and never the default; `LUMMENNA_SOVEREIGN` semantics
      and the documented local-first claim remain accurate afterwards
- [ ] Documented in `INTEGRATIONS.md` with a worked remote/Docker example, and
      the "MCP server is stdio-only" limitation removed **in the same change**
      rather than left contradicting the code
- [ ] `lummenna mcp --check` extended to diagnose the HTTP path (bind address,
      auth configured, reachable endpoint) with the same fail-closed semantics

---

## Enterprise Pro Roadmap — NOT YET AVAILABLE

These are **planned** commercial (Pro) capabilities. They are **not built and not
available today**, and they must never be advertised, gated, or claimed as
present until delivered (see `AGENTS.md`). They are registered in
`PLANNED_PRO_FEATURES` in `lummenna/licensing.py` — deliberately separate from the
enforceable `PRO_FEATURES` registry — so nothing here can be sold as a live
feature. Calling `assert_licensed(<planned-key>)` intentionally raises, because
gating an unbuilt capability would be a false availability claim.

When an item below is delivered to its acceptance criteria, in the **same
change**: (1) implement it completely (no stubs), (2) add its `assert_licensed`
gate at the real code boundary with tests proving Community is blocked and Pro is
allowed, (3) move its key from `PLANNED_PRO_FEATURES` to `PRO_FEATURES`, and (4)
move it into the "Available now" table of `COMMERCIAL-LICENSE.md`.

Current status of every item: **Not started** (design only).

### EP-P3 — Advanced API auth (`advanced_auth`)
RBAC, OIDC/JWT, per-user API keys, session/SSO. Replaces the single shared
`X-API-Key`.
- [ ] Role model (roles, permissions, role→permission mapping) persisted and migration-managed
- [ ] Per-user API keys: issue, rotate, revoke; keys stored only as salted hashes
- [ ] OIDC/JWT bearer validation (issuer, audience, expiry, signature via JWKS) with clock-skew tolerance
- [ ] Authorization enforced at every API route (deny-by-default); tests prove each route rejects insufficient scope
- [ ] Audit log entry for every auth decision (grant/deny) with actor, resource, decision
- [ ] Gated by `assert_licensed("advanced_auth")`; Community keeps the existing single-key auth

### EP-P5 — Scale-out backend (`scale_out`)
Postgres/pgvector backend with replication and HA, as an alternative to the
single-node SQLite store.
- [ ] Storage abstraction so retrieval/store paths are backend-agnostic (SQLite | Postgres+pgvector)
- [ ] Postgres schema + migrations at parity with the SQLite schema (rooms, loci, chunks, provenance, feedback, tenant_id)
- [ ] pgvector ANN index with recall parity to `sqlite-vec` on the committed benchmark set (documented R@k delta ≤ agreed threshold)
- [ ] Connection pooling, statement timeouts, and read-replica routing for read paths
- [ ] Backend matrix in CI: full test suite green on both SQLite and Postgres
- [ ] Gated by `assert_licensed("scale_out")`

### EP-V1 — Team dashboard & analytics (`team_analytics`)
Multi-user/tenant analytics beyond the single-node effectiveness dashboard.
- [ ] Per-tenant/per-user usage, retrieval-quality, and memory-lifecycle metrics with time-range queries
- [ ] Aggregation queries bounded and indexed (no full-table scans on the hot path)
- [ ] Access-controlled endpoints (requires `advanced_auth`) — a tenant sees only its own data
- [ ] Exportable (CSV/JSON) with documented schema
- [ ] Gated by `assert_licensed("team_analytics")`

### EP-V2 — Managed key vault / HSM (`key_vault`)
External KMS/HSM integration for encryption and signing keys (vs. local key
material only).
- [ ] Pluggable KMS provider interface with at least one real backend (e.g. cloud KMS or PKCS#11 HSM)
- [ ] Envelope encryption: data keys wrapped by a KMS-held master key; plaintext keys never persisted
- [ ] Key rotation without data re-encryption downtime; old keys retained for decrypt only
- [ ] **Encryption-at-rest key rotation** — rekey an existing store (SQLCipher `PRAGMA rekey` + Fernet field re-wrap) without data loss. Today P2 provides PBKDF2 key *derivation* only; rotating `LUMMENNA_ENCRYPTION_KEY` does not re-encrypt existing data, so it is not yet supported.
- [ ] Fail-closed if the vault is unreachable; explicit, tested error paths
- [ ] Gated by `assert_licensed("key_vault")`

### EP-V3 — Compliance reporting & exports (`compliance_reporting`)
GDPR/SOC2-oriented report generation and evidence exports.
- [ ] RTBF/erasure reports: what was erased, when, by whom, with tamper-evident audit references
- [ ] Data-subject access export (all data for a subject/tenant) in a documented machine-readable format
- [ ] Access/audit-trail exports over a selectable time range
- [ ] Deterministic, reproducible report artifacts with a documented schema and integrity hash
- [ ] Gated by `assert_licensed("compliance_reporting")`

### EP-V4 — SSO / directory sync (`sso_directory_sync`)
OIDC/SAML SSO plus SCIM directory synchronization.
- [ ] SAML 2.0 and OIDC login flows (SP-initiated), signature/assertion validation, replay protection
- [ ] SCIM 2.0 user/group provisioning and deprovisioning endpoints
- [ ] Just-in-time provisioning mapped onto the `advanced_auth` role model
- [ ] Deprovisioning revokes access immediately; tested end-to-end
- [ ] Gated by `assert_licensed("sso_directory_sync")`

### EP-V6 — Backup/restore orchestration & PITR (`backup_pitr`)
Point-in-time recovery beyond the current best-effort file-copy helper.
- [ ] Continuous WAL/changelog archiving with a documented retention policy
- [ ] Restore to an arbitrary timestamp within the retention window, verified by an automated restore test
- [ ] Backup integrity verification (checksums) and restore-drill automation in CI
- [ ] Encrypted backups at rest (integrates with `key_vault` when present)
- [ ] Gated by `assert_licensed("backup_pitr")`

### EP-V7 — Horizontal read scaling & HA topology (`ha_topology`)
Read-replica fan-out and failover (builds on `scale_out`).
- [ ] Read-replica routing with health checks and automatic failover
- [ ] Documented consistency model (replica lag bounds) and per-query routing controls
- [ ] Chaos/failover test proving no data loss and bounded recovery time on primary failure
- [ ] Gated by `assert_licensed("ha_topology")`

### EP-P6+ — Advanced P2P hardening (extends the shipped `advanced_p2p`)
Today `advanced_p2p` ships **live encryption/signing key-rotation**
(`BeamNode.rotate_key`) and the **durable replay-nonce store**
(`lummenna/p2p/replay.py`). The remaining items below are **not built** — the Beam
transport is still flat mDNS discovery with direct 1-hop TCP delivery. These
extend the same gate when delivered.
- [x] **Persistent replay store** — `SqliteNonceStore` keeps the seen-nonce set in
      the Lummenna database (`LUMMENNA_P2P_REPLAY_STORE=persistent`, migration
      0006), so it survives restarts and is shared across processes. Uniqueness is
      decided by a SQLite primary-key conflict, so concurrent deliveries of the
      same frame cannot both be accepted, and an unreachable store rejects the
      frame rather than accepting it. Retention: a nonce is kept until
      `frame_timestamp + LUMMENNA_P2P_REPLAY_WINDOW_SECONDS`, past which the
      timestamp check rejects the frame regardless. Eviction: expired rows are
      swept on an amortised schedule; `LUMMENNA_P2P_REPLAY_MAX_ENTRIES` caps the
      table under flood, discarding nearest-expiry first and logging at WARNING.
      Community keeps the process-local store, which is unchanged and ungated.
- [x] Adversarial-network test coverage for the delivered items —
      `tests/test_p2p_replay_store.py` covers capture-and-retransmit against the
      real frame path, retransmission after a listener restart, cross-process and
      cross-thread races on one nonce, forged and expired frames, flood-driven
      eviction, and fail-closed behaviour on store outage
- [ ] **Multi-node mesh routing** — multi-hop relay/forwarding across peers with a
      documented topology and partition handling (currently 1-hop only)
- [ ] Key-rotation handshake so peers negotiate the new key rather than relying on
      out-of-band agreement
- [ ] Adversarial-network test coverage for the two items above

---

## Honest Claim Posture

What Lummenna is, stated without inflation:

> Lummenna is a working, unreleased framework for sovereign agentic memory, with
> lifecycle management and native integrations. It runs entirely on-device and is
> single-node by design, because the store is single-writer SQLite. Retrieval
> quality is validated against synthetic corpora in controlled settings; no
> BEIR/MTEB leaderboard run has been published, so no competitive claim is made
> against other systems. Nothing has been tagged, published to PyPI, or released
> as an image.

Earlier revisions of this section described the project as "production-grade".
That was not supportable: the same repository describes its own Azure surface as
a "demo/leaderboard surface" (`infra/azure/main.bicep`), pins replicas to one,
and keeps the database on ephemeral local disk. Capability claims must match the
code, per `AGENTS.md` Rule 2.

This roadmap tracks the work required to reach the first stable public release, and
the continued improvement planned beyond it.
