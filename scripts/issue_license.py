# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-QuantumindSSI-Commercial
# Copyright (c) 2025-2026 QuantumindSSI. All rights reserved.
"""QuantumindSSI offline license-key signer.

Mints Lumena Pro license keys by signing a JSON payload with the QuantumindSSI
Ed25519 **private** key. The private key MUST be kept in an offline signing
vault and is NEVER committed to this repository. Only the matching public key is
embedded in ``lumena/licensing.py`` for offline verification.

Key format (must match ``lumena.licensing.parse_license_key``)::

    LUMENA-<base64url(payload_json)>.<base64url(ed25519_signature)>

Usage
-----
Provide the private key via ``--private-key-file`` (raw-32 base64url, one line)
or the ``LUMENA_SIGNING_PRIVATE_KEY_B64`` environment variable::

    python scripts/issue_license.py \
        --subject "Acme Corp" \
        --features "*" \
        --seats 10 \
        --expires 2027-08-15 \
        --private-key-file /path/to/quantumindssi-license-private-key.b64

    # or a specific feature subset:
    python scripts/issue_license.py --subject "Acme" \
        --features compliance,encryption_at_rest --expires 2027-01-01

The script prints the signed ``LUMENA-...`` key to stdout. It also verifies the
key against the embedded public key before printing, so a mismatched keypair
fails loudly instead of shipping an unusable license.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import uuid
from datetime import datetime


def _b64url_encode(data: bytes) -> str:
    """Base64url without padding, matching lumena.licensing._b64url_decode."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _load_private_key_bytes(args: argparse.Namespace) -> bytes:
    raw_b64: str | None = None
    if args.private_key_file:
        with open(args.private_key_file, encoding="utf-8") as fh:
            raw_b64 = fh.read().strip()
    elif os.environ.get("LUMENA_SIGNING_PRIVATE_KEY_B64"):
        raw_b64 = os.environ["LUMENA_SIGNING_PRIVATE_KEY_B64"].strip()
    else:
        sys.exit(
            "error: no private key provided. Use --private-key-file or set "
            "LUMENA_SIGNING_PRIVATE_KEY_B64."
        )
    try:
        key_bytes = _b64url_decode(raw_b64)
    except Exception as exc:  # noqa: BLE001 - surface any decode error clearly
        sys.exit(f"error: could not base64url-decode the private key: {exc}")
    if len(key_bytes) != 32:
        sys.exit(f"error: expected a raw 32-byte Ed25519 private key, got {len(key_bytes)} bytes.")
    return key_bytes


def _validate_expires(value: str) -> str:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        sys.exit(f"error: --expires must be YYYY-MM-DD, got {value!r}")
    return value


def build_payload(args: argparse.Namespace) -> dict:
    features = [f.strip() for f in args.features.split(",") if f.strip()]
    payload: dict[str, object] = {
        "sub": args.subject,
        "edition": "pro",
        "features": features,
        "iss": "QuantumindSSI",
        "id": args.license_id or str(uuid.uuid4()),
    }
    if args.seats:
        payload["seats"] = args.seats
    if args.expires:
        payload["exp"] = args.expires
    return payload


def sign_license(payload: dict, private_key_bytes: bytes) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    # Canonical, stable JSON so the signed bytes are reproducible.
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = priv.sign(payload_bytes)
    return f"LUMENA-{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"


def _self_verify(key: str) -> None:
    """Fail loudly if the minted key does not verify with the embedded pubkey."""
    # Import here so the script can run even if the package import path differs;
    # add the repo root to sys.path for direct invocation.
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from lumena.licensing import clear_license_cache, parse_license_key
    except Exception as exc:  # noqa: BLE001
        print(
            f"warning: could not import lumena.licensing to self-verify ({exc}). "
            "Key was signed but not verified against the embedded public key.",
            file=sys.stderr,
        )
        return
    clear_license_cache()
    try:
        lic = parse_license_key(key)
    except Exception as exc:  # noqa: BLE001
        sys.exit(
            f"error: minted key failed verification against the embedded public "
            f"key ({exc}). The private key likely does not match "
            f"lumena.licensing._QUANTUMINDSSI_PUBLIC_KEY_B64."
        )
    print(
        f"verified: edition={lic.edition} subject={lic.subject!r} "
        f"features={sorted(lic.features)} expires={lic.expires}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Mint a Lumena Pro license key (offline).")
    parser.add_argument("--subject", required=True, help="Licensee / organization name.")
    parser.add_argument(
        "--features",
        default="*",
        help="Comma-separated Pro feature keys, or '*' for all (default: '*').",
    )
    parser.add_argument("--seats", type=int, default=0, help="Informational seat count.")
    parser.add_argument(
        "--expires",
        type=_validate_expires,
        default=None,
        help="Expiry date YYYY-MM-DD (omit for perpetual).",
    )
    parser.add_argument("--license-id", default="", help="Explicit license id (default: uuid4).")
    parser.add_argument(
        "--private-key-file",
        default=None,
        help="Path to the raw-32 base64url Ed25519 private key.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip self-verification against the embedded public key.",
    )
    args = parser.parse_args(argv)

    private_key_bytes = _load_private_key_bytes(args)
    payload = build_payload(args)
    key = sign_license(payload, private_key_bytes)

    if not args.no_verify:
        _self_verify(key)

    # The key itself goes to stdout (pipe-friendly); diagnostics to stderr.
    print(key)


if __name__ == "__main__":
    main()
