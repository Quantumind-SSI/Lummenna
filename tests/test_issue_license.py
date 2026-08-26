"""Tests for the offline license tooling (scripts/issue_license.py).

Covers keypair generation, the keygen->issue->verify round trip, private-key
file permissions, and backward-compatible invocation.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lummenna import licensing

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "issue_license.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("issue_license", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_generate_keypair_is_valid_ed25519():
    mod = _load_module()
    priv_b64, pub_b64 = mod.generate_keypair()

    # Both halves decode to raw 32-byte keys.
    assert len(mod._b64url_decode(priv_b64)) == 32
    assert len(mod._b64url_decode(pub_b64)) == 32
    # Two calls produce different keys.
    priv2, pub2 = mod.generate_keypair()
    assert priv2 != priv_b64
    assert pub2 != pub_b64


def test_keygen_issue_verify_round_trip(monkeypatch):
    """A key minted with a generated private half verifies against its public half."""
    mod = _load_module()
    priv_b64, pub_b64 = mod.generate_keypair()

    # Embed the generated public key so the engine verifies against it.
    monkeypatch.setattr(licensing, "_QUANTUMINDSSI_PUBLIC_KEY_B64", pub_b64)
    licensing.clear_license_cache()

    priv_bytes = mod._b64url_decode(priv_b64)
    payload = {
        "sub": "Round Trip Inc",
        "edition": "pro",
        "features": ["compliance", "multi_tenant"],
        "iss": "QuantumindSSI",
        "id": "rt-1",
        "seats": 3,
    }
    key = mod.sign_license(payload, priv_bytes)

    lic = licensing.parse_license_key(key)
    assert lic.is_pro
    assert lic.subject == "Round Trip Inc"
    assert lic.seats == 3
    assert lic.grants("compliance")
    assert lic.grants("multi_tenant")
    assert not lic.grants("advanced_p2p")


def test_keygen_writes_private_key_0600(tmp_path):
    mod = _load_module()
    out = tmp_path / "priv.b64"

    class _Args:
        pass

    args = _Args()
    args.out = str(out)
    args.force = False
    mod.cmd_keygen(args)  # writes private key, prints public to stdout

    assert out.exists()
    # POSIX permission bits are only meaningful on POSIX platforms; on Windows
    # os.chmod cannot express 0600, so assert the strict mode only where it applies.
    if os.name == "posix":
        assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert len(mod._b64url_decode(out.read_text().strip())) == 32


def test_keygen_refuses_to_overwrite_without_force(tmp_path):
    mod = _load_module()
    out = tmp_path / "priv.b64"
    out.write_text("existing")

    class _Args:
        pass

    args = _Args()
    args.out = str(out)
    args.force = False
    with pytest.raises(SystemExit):
        mod.cmd_keygen(args)
    # Untouched.
    assert out.read_text() == "existing"


def test_cli_keygen_prints_public_key_to_stdout():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "keygen"],
        capture_output=True,
        text=True,
        check=True,
    )
    mod = _load_module()
    pub = result.stdout.strip()
    # stdout is exactly the public key (pipe-friendly); private goes to stderr.
    assert len(mod._b64url_decode(pub)) == 32
    assert "PRIVATE KEY" in result.stderr


def test_cli_backward_compatible_issue_without_subcommand(tmp_path, monkeypatch):
    """`issue_license.py --subject ...` (no subcommand) still mints a key."""
    mod = _load_module()
    priv_b64, pub_b64 = mod.generate_keypair()
    key_file = tmp_path / "priv.b64"
    key_file.write_text(priv_b64)

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--subject",
            "Legacy Corp",
            "--features",
            "compliance",
            "--private-key-file",
            str(key_file),
            "--no-verify",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    key = result.stdout.strip()
    assert key.startswith("LUMENA-")

    monkeypatch.setattr(licensing, "_QUANTUMINDSSI_PUBLIC_KEY_B64", pub_b64)
    licensing.clear_license_cache()
    lic = licensing.parse_license_key(key)
    assert lic.subject == "Legacy Corp"
    assert lic.grants("compliance")
