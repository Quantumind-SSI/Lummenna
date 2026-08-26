# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-QuantumindSSI-Commercial
# Copyright (c) 2025-2026 QuantumindSSI. All rights reserved.
"""QuantumindSSI offline license-key signer.

Mints Lumena Pro license keys by signing a JSON payload with the QuantumindSSI
Ed25519 **private** key. The private key MUST be kept in an offline signing
vault and is NEVER committed to this repository. Only the matching public key is
embedded in ``lummenna/licensing.py`` for offline verification.

Key format (must match ``lummenna.licensing.parse_license_key``)::

    LUMENA-<base64url(payload_json)>.<base64url(ed25519_signature)>

See ``docs/LICENSE_SIGNING_KEY_CUSTODY.md`` for the full generate / safeguard /
rotate / revoke runbook.

Usage
-----
Generate a signing keypair (offline, one-time)::

    python scripts/issue_license.py keygen --out quantumindssi-license-private-key.b64
    # prints the PUBLIC key to stdout for embedding in lummenna/licensing.py

Mint a license — provide the private key via ``--private-key-file`` (raw-32
base64url, one line) or the ``LUMENA_SIGNING_PRIVATE_KEY_B64`` environment
variable::

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
import stat
import sys
import uuid
from datetime import datetime


def _b64url_encode(data: bytes) -> str:
    """Base64url without padding, matching lummenna.licensing._b64url_decode."""
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
        from lummenna.licensing import clear_license_cache, parse_license_key
    except Exception as exc:  # noqa: BLE001
        print(
            f"warning: could not import lummenna.licensing to self-verify ({exc}). "
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
            f"lummenna.licensing._QUANTUMINDSSI_PUBLIC_KEY_B64."
        )
    print(
        f"verified: edition={lic.edition} subject={lic.subject!r} "
        f"features={sorted(lic.features)} expires={lic.expires}",
        file=sys.stderr,
    )


def generate_keypair() -> tuple[str, str]:
    """Generate a fresh Ed25519 keypair as (private_b64url, public_b64url).

    Both halves are raw 32-byte keys, base64url-encoded without padding — the
    exact format ``_load_private_key_bytes`` and
    ``lummenna.licensing._QUANTUMINDSSI_PUBLIC_KEY_B64`` expect.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    priv_raw = priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return _b64url_encode(priv_raw), _b64url_encode(pub_raw)


def _write_private_key(path: str, priv_b64: str) -> None:
    """Write the private key to ``path`` with owner-only (0600) permissions."""
    # Create with restrictive permissions from the start (avoid a window where
    # the secret is world-readable between open() and chmod()).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(priv_b64 + "\n")
    finally:
        # Ensure 0600 even if the file pre-existed with looser bits.
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def cmd_keygen(args: argparse.Namespace) -> None:
    """Generate a signing keypair and print the public key for embedding.

    The private key is written to ``--out`` (0600) if given, else printed to
    stderr with a loud warning. The public key is printed to stdout so it can be
    pasted into ``lummenna.licensing._QUANTUMINDSSI_PUBLIC_KEY_B64``.
    """
    priv_b64, pub_b64 = generate_keypair()

    if args.out:
        if os.path.exists(args.out) and not args.force:
            sys.exit(
                f"error: {args.out} already exists; refusing to overwrite a signing "
                f"key. Use --force only if you are certain."
            )
        _write_private_key(args.out, priv_b64)
        print(
            f"private key written to {args.out} (mode 0600). Move it to your offline "
            f"signing vault and DELETE any working copies.",
            file=sys.stderr,
        )
    else:
        print(
            "PRIVATE KEY (store in an offline vault; never commit; never log):",
            file=sys.stderr,
        )
        print(priv_b64, file=sys.stderr)

    print(
        "public key — paste into lummenna/licensing.py " "_QUANTUMINDSSI_PUBLIC_KEY_B64:",
        file=sys.stderr,
    )
    # The public key is the one artifact meant for stdout (pipe-friendly).
    print(pub_b64)


def _add_issue_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--subject", required=True, help="Licensee / organization name.")
    parser.add_argument(
        "--features",
        default="*",
        help="Comma-separated Pro feature keys, or '*' for all (default: '*').",
    )
    parser.add_argument(
        "--seats",
        type=int,
        default=0,
        help="Max concurrent non-default tenants (0 = unlimited).",
    )
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


def cmd_issue(args: argparse.Namespace) -> None:
    """Mint and print a signed Lumena Pro license key."""
    private_key_bytes = _load_private_key_bytes(args)
    payload = build_payload(args)
    key = sign_license(payload, private_key_bytes)

    if not args.no_verify:
        _self_verify(key)

    # The key itself goes to stdout (pipe-friendly); diagnostics to stderr.
    print(key)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="issue_license.py",
        description="Lumena Pro license tooling (offline): generate a signing "
        "keypair (keygen) or mint a license key (issue).",
    )
    sub = parser.add_subparsers(dest="command")

    p_keygen = sub.add_parser("keygen", help="Generate a new Ed25519 signing keypair (offline).")
    p_keygen.add_argument(
        "--out",
        default=None,
        help="Write the private key to this file (mode 0600). Omit to print to stderr.",
    )
    p_keygen.add_argument(
        "--force",
        action="store_true",
        help="Overwrite --out if it already exists (dangerous).",
    )
    p_keygen.set_defaults(func=cmd_keygen)

    p_issue = sub.add_parser("issue", help="Mint a Lumena Pro license key.")
    _add_issue_arguments(p_issue)
    p_issue.set_defaults(func=cmd_issue)

    # Backward-compatible default: if no subcommand is given, treat the invocation
    # as `issue` (the historical single-command behaviour, e.g.
    # `issue_license.py --subject "Acme" ...`).
    argv = list(sys.argv[1:] if argv is None else argv)
    known_commands = {"keygen", "issue"}
    if not argv or argv[0] not in known_commands:
        if argv and argv[0] in ("-h", "--help"):
            parser.parse_args(argv)  # prints top-level help and exits
            return
        argv = ["issue", *argv]

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
