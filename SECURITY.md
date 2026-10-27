# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 1.0.x | Security patches and bug fixes |
| 0.2.x (beta) | Security patches only |
| 0.1.x (alpha) | Not supported |
| < 0.1.0 | Not supported |

## Reporting a Vulnerability

Lummenna takes security seriously. If you discover a vulnerability, please report it via the GitHub Security Advisory page or email.

**Report via:** https://github.com/Quantumind-SSI/Lummenna/security/advisories/new

**PGP Key:** For sensitive disclosures, encrypt emails to:
```
security@lummenna.ai
```

Public key (ED25519):
```
-----BEGIN PGP PUBLIC KEY BLOCK-----

mDMEanMX+xYJKwYBBAHaRw8BAQdAiouSUIUCncVCqE4b1SGOPOhxSdLtEFpEyCf9
wIayqQi0J0x1bWVuIFNlY3VyaXR5IFRlYW0gPHNlY3VyaXR5QGx1bWVuLmFpPoiZ
BBMWCgBBFiEEu67vrpF7GiE9+8oCvCSdz9W9rjEFAmpzF/sCGwMFCQPCZwAFCwkI
BwICIgIGFQoJCAsCBBYCAwECHgcCF4AACgkQvCSdz9W9rjHYoAD+Kc+kZLioe+a/
r3x0gJAwcquZkvICU/HqbuH2NWmjo0gA/A9RSYJ3cm7EcskEzfwbVTJSD75N2xMB
WQYWQOYpjP4A
=iER2
-----END PGP PUBLIC KEY BLOCK-----
```

### What to Include

1. Description of the vulnerability
2. Steps to reproduce
3. Affected versions
4. Potential impact assessment
5. Suggested fix (if you have one)

### Response Timeline

| Phase | Timeframe |
|---|---|
| Acknowledgment | Within 48 hours |
| Initial assessment | Within 7 days |
| Patch or mitigation | Within 30 days (critical: 7 days) |
| Public disclosure | Coordinated with reporter |

## Security Design Principles

### Sovereign-by-Default

- Lummenna **never** sends data to external APIs unless explicitly configured
- `LUMMENNA_SOVEREIGN=true` (default) blocks P2P sharing, model downloads from HuggingFace, and clamps API CORS to localhost origins
- Embedding models run locally via ONNX Runtime

### Forgetting as Security

- Biologically-grounded forgetting prevents indefinite data retention
- Compliance module supports safety-triggered deletion (PII, secrets)
- Provenance chains track data lineage for audit

### Storage Security

- SQLite WAL mode with configurable sync levels
- File permissions enforced: `~/.lummenna` should be `700`
- Audit logs are stored in the SQLite `audit_log` table with structured entries

## Encryption at Rest

Encryption at rest is **implemented but opt-in** (`LUMMENNA_DATABASE_ENCRYPTION_MODE`, default `none`):

- **`sqlcipher`** — full-database encryption via SQLCipher, with PBKDF2 passphrase derivation. Requires the `sqlcipher3` package and `LUMMENNA_ENCRYPTION_KEY`.
- **`fernet`** — field-level encryption of chunk content via `cryptography.Fernet`. Requires `LUMMENNA_ENCRYPTION_KEY` (startup fails without it). **Caveat:** the FTS5 lexical index is built from plaintext so BM25 search keeps working — token-level plaintext therefore remains in the `chunk_fts` index on disk. If that is unacceptable for your threat model, use `sqlcipher` mode, which encrypts every page including the FTS index.
- **`none`** (default) — plaintext SQLite. Use OS-level disk encryption (FileVault, BitLocker, LUKS) if you cannot enable one of the modes above.

## P2P Transport Security

Beam P2P sharing is **encrypted and authenticated by default**: every frame is AES-256-GCM encrypted and HMAC-SHA256 signed, with a timestamp window plus a seen-nonce set for replay protection. A Beam listener refuses to start without `LUMMENNA_P2P_ENCRYPTION_KEY` unless `LUMMENNA_P2P_INSECURE=1` is explicitly set (not recommended).

Where the seen-nonce set lives is selected by `LUMMENNA_P2P_REPLAY_STORE`:

- **`memory`** (default, Community) — the set is held in the process, so a restart empties it. A frame captured shortly before a restart can be replayed once afterwards, for whatever remains of its timestamp window.
- **`persistent`** (Pro, `advanced_p2p`) — the set is held in the Lummenna database, so it survives restarts and is shared by every process using that store. Uniqueness is decided by a SQLite primary-key conflict, so concurrent deliveries of the same frame cannot both be accepted.

In either mode a nonce is retained for exactly `LUMMENNA_P2P_REPLAY_WINDOW_SECONDS` (default 30) past its frame's timestamp — beyond that the timestamp check rejects the frame anyway — and the set is capped at `LUMMENNA_P2P_REPLAY_MAX_ENTRIES` (default 100,000), with overflow evicted nearest-expiry first and logged at WARNING. If the store cannot be consulted, the frame is **rejected** and the failure is logged at ERROR; replay checking is never skipped.

Remaining caveats: key distribution between peers is manual, retention is measured against the local clock (a backwards clock jump larger than the window can make pruned nonces acceptable again, so keep the node on NTP), and rotating a key does not notify peers.

## License Signing Key

Lummenna Pro entitlements are authorised by an **Ed25519 signing key**; only the
matching public key is embedded in the shipped code (`lummenna/licensing.py`), and
verification is fully offline. The private signing key is the root of trust for
all commercial licenses and is **never committed to this repository**. Its
generation, storage, rotation, and revocation are governed by an internal,
access-controlled custody runbook that is intentionally not published.
Tooling lives in `scripts/issue_license.py` (`keygen` and `issue` subcommands).

## Known Limitations

1. **Encryption at rest is off by default** — see above; plaintext SQLite unless a mode is configured.
2. **API authentication is opt-in** — `X-API-Key` header validation is implemented but disabled when `LUMMENNA_API_KEY` is unset. Set it before exposing the server to any network. The HTML shell at `/` and `/dashboard`, health/readiness probes, API reference, OpenAPI schema, and aggregate `/metrics` endpoint remain public. `/v1/dashboard-data` is protected when an API key is configured because it exposes tenant-scoped room names, chunk counts, and feedback statistics.
3. **Rate limiting covers write/search endpoints only** — the five POST endpoints are rate-limited; GET endpoints are not.
4. **Sovereign mode blocks P2P and downloads** — When `LUMMENNA_SOVEREIGN=true` (default), P2P Beam sharing and HuggingFace model downloads are blocked with `SovereignViolationError`. The API CORS policy is also clamped to localhost origins. Sovereign mode does not sandbox the host OS or prevent other non-Lummenna processes from making network calls.
5. **P2P replay protection is process-local by default** — with `LUMMENNA_P2P_REPLAY_STORE=memory` (the Community default) the seen-nonce set is not persisted across restarts; the timestamp window still applies. Durable, restart-surviving replay protection is available as Pro (`advanced_p2p`) via `LUMMENNA_P2P_REPLAY_STORE=persistent`.

## Dependency Security

We monitor dependencies for known vulnerabilities:

```bash
# Audit dependencies
pip install pip-audit
pip-audit --desc
```

Key dependencies and their security posture:

| Dependency | Criticality | Notes |
|---|---|---|
| SQLite | High | Built-in, audited by SQLite consortium |
| ONNX Runtime | Medium | No network code; models are local files |
| USearch | Medium | No network code; memory-mapped files |
| FastAPI / Uvicorn | Medium | Only exposed if API server enabled |

---

*Last updated: 2026-08-05*  
*Maintainer: Lummenna Security Team*
