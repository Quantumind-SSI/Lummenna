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


def _mint(priv, *, features=("*",), edition="pro", exp=None, subject="Test Corp") -> str:
    payload: dict[str, object] = {
        "sub": subject,
        "edition": edition,
        "features": list(features),
        "iss": "QuantumindSSI",
        "id": "test-license",
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
