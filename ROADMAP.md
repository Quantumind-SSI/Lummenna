# Lumena Production Readiness Roadmap

This document tracks ongoing and future work for Lumena following the v1.0.0 public release.

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
- [x] File permissions enforcement: `~/.lumena` is `700` by default

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
- [x] Transport encryption — AES-256-GCM + HMAC-SHA256 with replay protection shipped in `lumena/p2p/beam.py` / `lumena/security/crypto.py`; a Beam listener refuses to start without a key
- [ ] Harden Beam protocol for adversarial networks (persistent replay-nonce store, key rotation) — target v0.3.0
- [x] Document trust model and household-local limitation

### 3.3 Release Engineering
- [x] PGP key for vulnerability reporting — generated and published in SECURITY.md
- [ ] CI/CD: add `pip-audit`, fuzz tests, backend matrix tests
- [x] Tag `v0.2.0-beta`
- [x] Tag `v1.0.0` — inaugural public release
- [x] Publish release notes with closed/open gaps

---

## Enterprise Pro Roadmap — NOT YET AVAILABLE

These are **planned** commercial (Pro) capabilities. They are **not built and not
available today**, and they must never be advertised, gated, or claimed as
present until delivered (see `AGENTS.md`). They are registered in
`PLANNED_PRO_FEATURES` in `lumena/licensing.py` — deliberately separate from the
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

---

## Honest Claim Posture

As stated in the whitepaper:

> Lumena is a production-grade framework for sovereign agentic memory with unique lifecycle management and native integrations; its retrieval quality is competitive in controlled settings and validated by synthetic benchmarks, with full BEIR/MTEB leaderboard evaluation as ongoing work.

This roadmap tracks continued improvement beyond the v1.0.0 public release.
