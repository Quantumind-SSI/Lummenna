"""Entitlement engine + Phase 3 Pro gating tests.

Covers:
  * Offline license-key signing/verification round-trip (no network).
  * Community (no key) is fail-closed; Pro (valid signed key) is entitled.
  * Invalid / tampered / expired keys resolve to Community.
  * The three Wave-1 Pro gates block in Community and allow with a Pro key:
    compliance (RTBF), encryption_at_rest, advanced_p2p (key rotation).

To avoid embedding the production private key, these tests generate an
ephemeral Ed25519 keypair and monkeypatch the embedded public key so the
entitlement engine verifies against the test key.
"""

from __future__ import annotations

import base64
import json

import pytest

from lumena import licensing
from lumena.brand.errors import LicenseRequiredError
from lumena.config import LumenaConfig


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@pytest.fixture
def signing_keypair(monkeypatch):
    """Generate an ephemeral keypair and embed its public half for the engine."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    monkeypatch.setattr(licensing, "_QUANTUMINDSSI_PUBLIC_KEY_B64", _b64url(pub_raw))
    licensing.clear_license_cache()
    yield priv
    licensing.clear_license_cache()


def _mint(
    priv,
    *,
    features=("*",),
    edition="pro",
    exp=None,
    subject="Test Corp",
    license_id="test-license",
) -> str:
    payload: dict[str, object] = {
        "sub": subject,
        "edition": edition,
        "features": list(features),
        "iss": "QuantumindSSI",
        "id": license_id,
    }
    if exp:
        payload["exp"] = exp
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = priv.sign(payload_bytes)
    return f"LUMENA-{_b64url(payload_bytes)}.{_b64url(sig)}"


# --------------------------------------------------------------------------- #
# Core entitlement engine
# --------------------------------------------------------------------------- #


def test_no_key_is_community():
    licensing.clear_license_cache()
    cfg = LumenaConfig(license_key=None)
    assert cfg.edition == "community"
    assert not licensing.is_licensed("compliance", cfg)


def test_valid_pro_key_grants_all_features(signing_keypair):
    key = _mint(signing_keypair, features=["*"])
    cfg = LumenaConfig(license_key=key)
    assert cfg.edition == "pro"
    for feature in ("compliance", "encryption_at_rest", "advanced_p2p"):
        assert licensing.is_licensed(feature, cfg)
        cfg.assert_licensed(feature)  # must not raise


def test_feature_subset_key_grants_only_listed(signing_keypair):
    key = _mint(signing_keypair, features=["compliance"])
    cfg = LumenaConfig(license_key=key)
    assert licensing.is_licensed("compliance", cfg)
    assert not licensing.is_licensed("encryption_at_rest", cfg)
    with pytest.raises(LicenseRequiredError):
        cfg.assert_licensed("encryption_at_rest")


def test_tampered_key_falls_back_to_community(signing_keypair):
    key = _mint(signing_keypair, features=["*"])
    # Flip a character in the payload segment -> signature no longer verifies.
    body = key[len("LUMENA-") :]
    payload_seg, sig_seg = body.split(".", 1)
    tampered = (
        "LUMENA-" + payload_seg[:-1] + ("A" if payload_seg[-1] != "A" else "B") + "." + sig_seg
    )
    cfg = LumenaConfig(license_key=tampered)
    assert cfg.edition == "community"


def test_expired_key_is_not_pro(signing_keypair):
    key = _mint(signing_keypair, features=["*"], exp="2000-01-01")
    cfg = LumenaConfig(license_key=key)
    # Expired -> load resolves to Community (fail-closed), assert raises.
    assert cfg.edition == "community"
    with pytest.raises(LicenseRequiredError):
        cfg.assert_licensed("compliance")


def test_unknown_feature_key_is_programming_error(signing_keypair):
    key = _mint(signing_keypair, features=["*"])
    cfg = LumenaConfig(license_key=key)
    with pytest.raises(ValueError):
        cfg.assert_licensed("not_a_real_feature")


# --------------------------------------------------------------------------- #
# Fail-closed on adversarial payload coercion (audit finding 1 + 2)
# --------------------------------------------------------------------------- #


def _mint_raw(priv, payload: dict) -> str:
    """Sign an arbitrary payload dict (allows malformed fields for fuzzing)."""
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return f"LUMENA-{_b64url(payload_bytes)}.{_b64url(priv.sign(payload_bytes))}"


@pytest.mark.parametrize(
    "bad",
    [
        {"seats": "notanint"},  # non-numeric string -> was ValueError
        {"seats": -5},  # negative -> was silently "unlimited"
        {"seats": True},  # bool masquerading as int
        {"seats": [1]},  # wrong type
        {"features": 5},  # non-iterable -> was TypeError
        {"features": {"x": 1}},  # dict -> was TypeError on frozenset
        {"features": ["ok", 3]},  # mixed non-str members
    ],
)
def test_adversarial_payload_raises_license_invalid(signing_keypair, bad):
    """A validly-signed but malformed payload must raise LicenseInvalidError,

    never an uncaught ValueError/TypeError. This is what lets the engine
    fail-closed to Community instead of crashing the caller.
    """
    from lumena.brand.errors import LicenseInvalidError

    payload = {
        "sub": "t",
        "edition": "pro",
        "features": ["*"],
        "iss": "QuantumindSSI",
        "id": "adv",
        **bad,
    }
    key = _mint_raw(signing_keypair, payload)
    licensing.clear_license_cache()
    with pytest.raises(LicenseInvalidError):
        licensing.parse_license_key(key)


@pytest.mark.parametrize(
    "bad",
    [
        {"seats": "notanint"},
        {"seats": -5},
        {"features": 5},
        {"features": None},
    ],
)
def test_adversarial_payload_fails_closed_to_community(signing_keypair, bad):
    """The config resolution path must degrade to Community, not crash.

    Regression for the audit finding where a malformed 'seats'/'features' field
    escaped parse_license_key and crashed config.edition with ValueError/TypeError.
    """
    payload = {
        "sub": "t",
        "edition": "pro",
        "features": ["*"],
        "iss": "QuantumindSSI",
        "id": "adv2",
        **bad,
    }
    key = _mint_raw(signing_keypair, payload)
    licensing.clear_license_cache()
    cfg = LumenaConfig(license_key=key)
    # features=None is a *valid* empty-entitlement Pro key (grants nothing);
    # all the others are malformed and fail closed. Neither may raise here.
    if bad == {"features": None}:
        assert cfg.edition == "pro"
        assert not licensing.is_licensed("compliance", cfg)
    else:
        assert cfg.edition == "community"


def test_valid_seats_variants_accepted(signing_keypair):
    """Legitimate seat encodings still parse: numeric string, float, zero."""
    for raw, expected in (("10", 10), (3.7, 3), (0, 0)):
        payload = {
            "sub": "t",
            "edition": "pro",
            "features": ["*"],
            "iss": "QuantumindSSI",
            "id": "sv",
            "seats": raw,
        }
        licensing.clear_license_cache()
        lic = licensing.parse_license_key(_mint_raw(signing_keypair, payload))
        assert lic.seats == expected


def test_string_features_treated_as_single_feature(signing_keypair):
    """A bare-string 'features' must be one feature, not a set of characters."""
    payload = {
        "sub": "t",
        "edition": "pro",
        "features": "compliance",
        "iss": "QuantumindSSI",
        "id": "sf",
    }
    licensing.clear_license_cache()
    lic = licensing.parse_license_key(_mint_raw(signing_keypair, payload))
    assert lic.features == frozenset({"compliance"})
    assert lic.grants("compliance")
    assert not lic.grants("c")  # not exploded into characters


def test_coerce_helpers_unit():
    """Direct unit coverage of the coercion helpers."""
    from lumena.brand.errors import LicenseInvalidError

    assert licensing._coerce_features(None) == frozenset()
    assert licensing._coerce_features("x") == frozenset({"x"})
    assert licensing._coerce_features(["a", "b"]) == frozenset({"a", "b"})
    with pytest.raises(LicenseInvalidError):
        licensing._coerce_features(5)
    with pytest.raises(LicenseInvalidError):
        licensing._coerce_features(["a", 3])

    assert licensing._coerce_seats(None) == 0
    assert licensing._coerce_seats("") == 0
    assert licensing._coerce_seats("7") == 7
    assert licensing._coerce_seats(3.9) == 3
    with pytest.raises(LicenseInvalidError):
        licensing._coerce_seats("nope")
    with pytest.raises(LicenseInvalidError):
        licensing._coerce_seats(-1)
    with pytest.raises(LicenseInvalidError):
        licensing._coerce_seats(True)


# --------------------------------------------------------------------------- #
# Offline revocation
# --------------------------------------------------------------------------- #


def test_revoked_license_id_resolves_to_community(signing_keypair, monkeypatch):
    """A validly-signed key whose id is revoked must not grant Pro."""
    key = _mint(signing_keypair, features=["*"], license_id="revoked-123")
    monkeypatch.setattr(licensing, "REVOKED_LICENSE_IDS", frozenset({"revoked-123"}))
    licensing.clear_license_cache()
    cfg = LumenaConfig(license_key=key)
    # Fail-closed: revoked key resolves to Community, and the Pro gate raises.
    assert cfg.edition == "community"
    with pytest.raises(LicenseRequiredError):
        cfg.assert_licensed("compliance")


def test_revocation_raises_specific_error_on_parse(signing_keypair, monkeypatch):
    from lumena.brand.errors import LicenseInvalidError

    key = _mint(signing_keypair, features=["*"], license_id="revoked-xyz")
    monkeypatch.setattr(licensing, "REVOKED_LICENSE_IDS", frozenset({"revoked-xyz"}))
    licensing.clear_license_cache()
    with pytest.raises(LicenseInvalidError, match="revoked"):
        licensing.parse_license_key(key)


def test_non_revoked_key_still_valid(signing_keypair, monkeypatch):
    """Revoking one id must not affect a different, valid key."""
    key = _mint(signing_keypair, features=["*"], license_id="good-key")
    monkeypatch.setattr(licensing, "REVOKED_LICENSE_IDS", frozenset({"some-other-id"}))
    licensing.clear_license_cache()
    cfg = LumenaConfig(license_key=key)
    assert cfg.edition == "pro"
    cfg.assert_licensed("compliance")  # must not raise


def test_no_public_key_embedded_is_fail_closed(monkeypatch, signing_keypair):
    # Even a correctly-signed key must not grant Pro if no public key is trusted.
    key = _mint(signing_keypair, features=["*"])
    monkeypatch.setattr(licensing, "_QUANTUMINDSSI_PUBLIC_KEY_B64", "")
    licensing.clear_license_cache()
    cfg = LumenaConfig(license_key=key)
    assert cfg.edition == "community"


# --------------------------------------------------------------------------- #
# Phase 3 gates
# --------------------------------------------------------------------------- #


def test_encryption_at_rest_gate_blocks_community(tmp_path):
    licensing.clear_license_cache()
    cfg = LumenaConfig(
        store_path=str(tmp_path / ".lumena"),
        database_encryption_mode="sqlcipher",
        encryption_key="x" * 16,
    )
    from lumena.data.schema import get_connection

    with pytest.raises(LicenseRequiredError):
        get_connection(cfg)


def test_encryption_none_works_in_community(tmp_path):
    licensing.clear_license_cache()
    cfg = LumenaConfig(
        store_path=str(tmp_path / ".lumena"),
        database_encryption_mode="none",
    )
    from lumena.data.schema import get_connection

    conn = get_connection(cfg)  # must NOT raise
    conn.close()


def test_compliance_rtbf_gate_blocks_community():
    import sqlite3

    licensing.clear_license_cache()
    from lumena.compliance.safety_forgetting import safety_forget_chunk

    with pytest.raises(LicenseRequiredError):
        safety_forget_chunk(sqlite3.connect(":memory:"), 1, "test")


def test_advanced_p2p_rotate_key_gate_blocks_community():
    licensing.clear_license_cache()
    cfg = LumenaConfig(p2p_encryption_key="k" * 32)
    from lumena.p2p.beam import BeamNode

    node = BeamNode(cfg)
    with pytest.raises(LicenseRequiredError):
        node.rotate_key("n" * 32)


def test_advanced_p2p_rotate_key_allowed_with_pro(signing_keypair):
    key = _mint(signing_keypair, features=["*"])
    cfg = LumenaConfig(license_key=key, p2p_encryption_key="k" * 32)
    from lumena.p2p.beam import BeamNode

    node = BeamNode(cfg)
    node.rotate_key("n" * 32)  # must NOT raise
    assert cfg.p2p_encryption_key == "n" * 32


# --------------------------------------------------------------------------- #
# Registry integrity — no vaporware, no false claims (AGENTS.md Rule 2/3)
# --------------------------------------------------------------------------- #


def test_pro_and_planned_registries_are_disjoint():
    """A feature is either enforceable now OR planned — never both."""
    overlap = set(licensing.PRO_FEATURES) & set(licensing.PLANNED_PRO_FEATURES)
    assert not overlap, f"features in both PRO_FEATURES and PLANNED_PRO_FEATURES: {overlap}"


def test_planned_feature_cannot_be_gated_as_available(signing_keypair):
    """Even with a wildcard Pro key, a planned feature must not silently gate.

    Gating a not-yet-built capability would be a false 'available' claim; the
    engine surfaces it as a programming error instead.
    """
    key = _mint(signing_keypair, features=["*"])
    cfg = LumenaConfig(license_key=key)
    for planned in licensing.PLANNED_PRO_FEATURES:
        with pytest.raises(ValueError, match="planned but not yet built"):
            cfg.assert_licensed(planned)


def test_every_enforceable_pro_feature_is_actually_gated():
    """Each code-backed PRO_FEATURES key must have a real assert_licensed gate.

    Non-code commercial entitlements (support/release commitments) are exempt:
    they are granted by license but have no code path to gate.
    """
    import pathlib

    non_code_entitlements = {"priority_support", "lts"}
    code_features = set(licensing.PRO_FEATURES) - non_code_entitlements

    root = pathlib.Path(__file__).resolve().parent.parent / "lumena"
    sources = "\n".join(
        p.read_text(encoding="utf-8") for p in root.rglob("*.py") if p.name != "licensing.py"
    )
    for feature in code_features:
        assert (
            f'assert_licensed("{feature}")' in sources or f"assert_licensed('{feature}')" in sources
        ), f"PRO_FEATURES key {feature!r} is declared but never gated by assert_licensed()"


# --------------------------------------------------------------------------- #
# multi_tenant (P4) gate — read + write boundaries
# --------------------------------------------------------------------------- #


def _tenant_cfg(tmp_path, license_key=None):
    return LumenaConfig(store_path=str(tmp_path / ".lumena"), license_key=license_key)


def test_multi_tenant_write_blocks_community(tmp_path):
    licensing.clear_license_cache()
    from lumena.data.schema import get_connection
    from lumena.force.mnemonic.store import _tenant_id_supported, store_memory

    cfg = _tenant_cfg(tmp_path)
    conn = get_connection(cfg)
    if not _tenant_id_supported(conn):
        pytest.skip("schema predates tenant support")
    try:
        with pytest.raises(LicenseRequiredError):
            store_memory(conn, "secret", "room", tenant_id="acme", config=cfg)
    finally:
        conn.close()


def test_default_tenant_write_is_free(tmp_path):
    licensing.clear_license_cache()
    from lumena.data.schema import get_connection
    from lumena.force.mnemonic.store import store_memory

    cfg = _tenant_cfg(tmp_path)
    conn = get_connection(cfg)
    try:
        cid = store_memory(conn, "hello world", "room", tenant_id="default", config=cfg)
        assert cid > 0  # community single-tenant path must NOT be gated
    finally:
        conn.close()


def test_multi_tenant_write_allowed_with_pro(tmp_path, signing_keypair):
    key = _mint(signing_keypair, features=["*"])
    from lumena.data.schema import get_connection
    from lumena.force.mnemonic.store import _tenant_id_supported, store_memory

    cfg = _tenant_cfg(tmp_path, license_key=key)
    conn = get_connection(cfg)
    if not _tenant_id_supported(conn):
        pytest.skip("schema predates tenant support")
    try:
        cid = store_memory(conn, "tenant secret", "room", tenant_id="acme", config=cfg)
        assert cid > 0  # Pro key must NOT raise
    finally:
        conn.close()


def test_multi_tenant_read_filter_blocks_community(tmp_path):
    licensing.clear_license_cache()
    from lumena.data.schema import get_connection
    from lumena.force.mnemonic.store import _tenant_id_supported

    cfg = _tenant_cfg(tmp_path)
    conn = get_connection(cfg)
    if not _tenant_id_supported(conn):
        pytest.skip("schema predates tenant support")

    pipeline = _SearchPipelineForTenant(conn, cfg)
    # _filter_by_tenant only reads .chunk_id; a light stub avoids constructing a
    # full RetrievedChunk (many required fields) unrelated to the gate under test.
    fake = [_ChunkStub(1)]
    try:
        with pytest.raises(LicenseRequiredError):
            pipeline._filter_by_tenant(fake, "acme")
        # default tenant must never be gated
        assert pipeline._filter_by_tenant(fake, "default") == fake
    finally:
        conn.close()


def test_seats_unlimited_when_zero(tmp_path, signing_keypair):
    """A licence without a seats field (seats == 0) is uncapped."""
    from lumena.data.schema import get_connection
    from lumena.force.mnemonic.store import _tenant_id_supported, store_memory

    key = _mint(signing_keypair, features=["*"])  # no seats -> 0 -> unlimited
    cfg = _tenant_cfg(tmp_path, license_key=key)
    conn = get_connection(cfg)
    if not _tenant_id_supported(conn):
        pytest.skip("schema predates tenant support")
    try:
        for i in range(5):
            store_memory(conn, "x", "r", tenant_id=f"T{i}", config=cfg)
        conn.commit()
        assert licensing.licensed_seats(cfg) == 0
    finally:
        conn.close()


def _mint_with_seats(priv, seats: int) -> str:
    payload = {
        "sub": "Seat Corp",
        "edition": "pro",
        "features": ["*"],
        "iss": "QuantumindSSI",
        "id": f"seat-{seats}",
        "seats": seats,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return f"LUMENA-{_b64url(payload_bytes)}.{_b64url(priv.sign(payload_bytes))}"


def test_seat_limit_blocks_new_tenant_over_cap(tmp_path, signing_keypair):
    from lumena.brand.errors import SeatLimitError
    from lumena.data.schema import get_connection
    from lumena.force.mnemonic.store import _tenant_id_supported, store_memory

    key = _mint_with_seats(signing_keypair, 2)
    cfg = _tenant_cfg(tmp_path, license_key=key)
    conn = get_connection(cfg)
    if not _tenant_id_supported(conn):
        pytest.skip("schema predates tenant support")
    try:
        store_memory(conn, "a", "r", tenant_id="A", config=cfg)
        store_memory(conn, "b", "r", tenant_id="B", config=cfg)
        conn.commit()
        assert licensing.licensed_seats(cfg) == 2
        # A third distinct tenant exceeds the 2 seats.
        with pytest.raises(SeatLimitError):
            store_memory(conn, "c", "r", tenant_id="C", config=cfg)
    finally:
        conn.close()


def test_seat_limit_allows_reuse_of_existing_tenant(tmp_path, signing_keypair):
    from lumena.data.schema import get_connection
    from lumena.force.mnemonic.store import _tenant_id_supported, store_memory

    key = _mint_with_seats(signing_keypair, 1)
    cfg = _tenant_cfg(tmp_path, license_key=key)
    conn = get_connection(cfg)
    if not _tenant_id_supported(conn):
        pytest.skip("schema predates tenant support")
    try:
        store_memory(conn, "a", "r1", tenant_id="solo", config=cfg)
        # Re-storing under the SAME tenant uses the existing seat, not a new one.
        store_memory(conn, "a2", "r2", tenant_id="solo", config=cfg)
        conn.commit()
    finally:
        conn.close()


def test_default_tenant_never_counts_against_seats(tmp_path, signing_keypair):
    from lumena.data.schema import get_connection
    from lumena.force.mnemonic.store import _tenant_id_supported, store_memory

    key = _mint_with_seats(signing_keypair, 1)
    cfg = _tenant_cfg(tmp_path, license_key=key)
    conn = get_connection(cfg)
    if not _tenant_id_supported(conn):
        pytest.skip("schema predates tenant support")
    try:
        store_memory(conn, "a", "r", tenant_id="only", config=cfg)  # fills the 1 seat
        # default tenant is always free and must not trip the seat limit.
        cid = store_memory(conn, "d", "r", tenant_id="default", config=cfg)
        conn.commit()
        assert cid > 0
    finally:
        conn.close()


def test_two_tenants_can_share_room_name(tmp_path, signing_keypair):
    """Regression for the migration-0004 defect: a global UNIQUE(room.name)

    previously made two tenants collide on a shared room name. After the
    tenant-scoped rebuild, both may use the same room name.
    """
    key = _mint(signing_keypair, features=["*"])
    from lumena.data.schema import get_connection
    from lumena.force.mnemonic.store import _tenant_id_supported, store_memory

    cfg = _tenant_cfg(tmp_path, license_key=key)
    conn = get_connection(cfg)
    if not _tenant_id_supported(conn):
        pytest.skip("schema predates tenant support")
    try:
        a = store_memory(conn, "alpha note", "notes", tenant_id="tenantA", config=cfg)
        b = store_memory(conn, "beta note", "notes", tenant_id="tenantB", config=cfg)
        conn.commit()
        assert a > 0 and b > 0 and a != b
        rooms = conn.execute(
            "SELECT tenant_id FROM room WHERE name = 'notes' ORDER BY tenant_id"
        ).fetchall()
        assert [r[0] for r in rooms] == ["tenantA", "tenantB"]
    finally:
        conn.close()


def test_migration_0004_makes_room_unique_tenant_scoped(tmp_path):
    """The room UNIQUE index must be (name, tenant_id), not global (name)."""
    from lumena.data.schema import get_connection
    from lumena.force.mnemonic.store import _tenant_id_supported

    cfg = _tenant_cfg(tmp_path)
    conn = get_connection(cfg)
    if not _tenant_id_supported(conn):
        pytest.skip("schema predates tenant support")
    try:
        unique_cols = [
            [row[2] for row in conn.execute(f"PRAGMA index_info({idx[1]})").fetchall()]
            for idx in conn.execute("PRAGMA index_list(room)").fetchall()
            if idx[2]  # unique
        ]
        assert ["name", "tenant_id"] in unique_cols
        # No lingering global UNIQUE(name)-only index.
        assert ["name"] not in unique_cols
        # Foreign keys survived the rebuild.
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_fusion_query_isolates_tenants(tmp_path, signing_keypair):
    """Query-level isolation: fuse_and_rerank must not return cross-tenant rows.

    Both tenants' chunks are handed to fusion as candidates (as the retrieval
    channels would, since they rank across the whole palace). The tenant
    predicate at the relational chokepoint must drop the other tenant's chunk
    *before* content is materialised/decrypted.
    """
    key = _mint(signing_keypair, features=["*"])
    from lumena.data.schema import get_connection
    from lumena.force.mnemonic.retrieval_lexical import LexicalHit
    from lumena.force.mnemonic.store import _tenant_id_supported, store_memory
    from lumena.fusion import fuse_and_rerank

    cfg = _tenant_cfg(tmp_path, license_key=key)
    conn = get_connection(cfg)
    if not _tenant_id_supported(conn):
        pytest.skip("schema predates tenant support")
    try:
        a = store_memory(conn, "alpha secret about apples", "notes", tenant_id="A", config=cfg)
        b = store_memory(conn, "beta secret about apples", "notes", tenant_id="B", config=cfg)
        conn.commit()

        # Both proposed as candidates, exactly as cross-tenant fusion would.
        candidates = [
            LexicalHit(chunk_id=a, rank=1.0, match_info=b""),
            LexicalHit(chunk_id=b, rank=1.0, match_info=b""),
        ]

        res_a = fuse_and_rerank(candidates, [], [], conn, tenant_id="A")
        res_b = fuse_and_rerank(candidates, [], [], conn, tenant_id="B")
        res_default = fuse_and_rerank(candidates, [], [], conn, tenant_id="default")

        assert [r.chunk_id for r in res_a] == [a]
        assert [r.chunk_id for r in res_b] == [b]
        # B's content must never surface in A's results.
        assert all("beta" not in (r.content or "") for r in res_a)
        # Default tenant is unrestricted (single-tenant deployments unaffected).
        assert sorted(r.chunk_id for r in res_default) == sorted([a, b])
    finally:
        conn.close()


def test_fusion_default_tenant_unrestricted_on_legacy_schema(memory_db):
    """On a schema WITH tenant support, default tenant still sees all chunks;

    the predicate is a no-op for 'default'. Uses the in-memory fixture to prove
    the fast path adds no restriction and no error.
    """
    import hashlib

    from lumena.force.mnemonic.retrieval_lexical import LexicalHit
    from lumena.fusion import fuse_and_rerank

    cur = memory_db.execute(
        "INSERT INTO room(name, room_type, tenant_id) VALUES('r','domain','default')"
    )
    rid = cur.lastrowid
    memory_db.execute(
        "CREATE TABLE IF NOT EXISTS vec_fallback (chunk_id INTEGER PRIMARY KEY, embedding BLOB)"
    )
    for i in (1, 2):
        content = f"chunk {i}"
        memory_db.execute(
            "INSERT INTO chunk(chunk_id, room_id, content, content_hash, created_at,"
            " vm_score, resolution, tenant_id) VALUES(?,?,?,?,unixepoch(),0.5,'FP32','default')",
            (i, rid, content, hashlib.sha256(content.encode()).hexdigest()),
        )
    memory_db.commit()
    candidates = [
        LexicalHit(chunk_id=1, rank=1.0, match_info=b""),
        LexicalHit(chunk_id=2, rank=1.0, match_info=b""),
    ]
    res = fuse_and_rerank(candidates, [], [], memory_db, tenant_id="default")
    assert sorted(r.chunk_id for r in res) == [1, 2]


class _ChunkStub:
    """Minimal stand-in exposing only the .chunk_id that _filter_by_tenant reads."""

    def __init__(self, chunk_id: int) -> None:
        self.chunk_id = chunk_id


def _SearchPipelineForTenant(conn, cfg):
    """Construct a SearchPipeline without triggering real embedder loading."""
    from lumena.search import SearchPipeline

    pipeline = SearchPipeline.__new__(SearchPipeline)
    pipeline.conn = conn
    pipeline.config = cfg
    return pipeline
