# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-QuantumindSSI-Commercial
# Copyright (c) 2025-2026 QuantumindSSI. All rights reserved.
"""D18: Licensing & Entitlement Engine.

Input wire: LUMENA_LICENSE_KEY / config.license_key
Output wire: every Pro-gated feature (compliance RTBF, encryption-at-rest,
             advanced auth, multi-tenant, scale-out, advanced P2P)
Secret sauce: offline, sovereign-friendly Ed25519-signed license keys — NO
              phone-home. A key is a base64url payload + detached signature,
              verified against an embedded QuantumindSSI public key.

Design goals
------------
* Sovereign: validation is fully offline. No network call, ever. This mirrors
  ``LumenaConfig.assert_sovereign`` — Pro must not betray the local-first ethos.
* Fail-closed for Pro: if no valid license is present, Pro features raise
  ``LicenseRequiredError``. Community features are never gated.
* Tamper-evident: keys are signed. A user cannot fabricate entitlements by
  editing an env var, because the signature would not verify.
* Graceful in dev: if the ``cryptography`` package or a real public key is not
  available, the engine treats the edition as Community (fail-closed), never
  as Pro.

License key format
------------------
``LUMENA-<base64url(payload_json)>.<base64url(ed25519_signature)>``

payload_json = {
    "sub":       "<licensee/org>",
    "edition":   "pro",
    "features":  ["*"] | ["compliance", "encryption_at_rest", ...],
    "seats":     10,                 # informational
    "exp":       "2027-08-15",       # ISO date; omit for perpetual
    "iss":       "QuantumindSSI",
    "id":        "<license-id>"
}
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from typing import TYPE_CHECKING

from lumena.brand.errors import LicenseInvalidError, LicenseRequiredError

if TYPE_CHECKING:
    from lumena.config import LumenaConfig


# ---------------------------------------------------------------------------
# Entitlements registry — the authoritative mapping of Pro feature keys.
# Mirrors COMMERCIAL-LICENSE.md. Community features are NOT listed here;
# only Pro-gated capabilities need an entry.
# ---------------------------------------------------------------------------

#: Canonical Pro feature keys that are **built and enforceable today**. Every
#: entry here MUST have a real implementation gated by ``assert_licensed(<key>)``
#: in the codebase (see AGENTS.md Rule 2). Referenced by ``assert_licensed``.
#:
#: Do NOT add a key here until its capability actually exists and is gated. A
#: declared-but-ungated feature is either a revenue leak or false advertising —
#: unbuilt work belongs in ``PLANNED_PRO_FEATURES`` and ``ROADMAP.md`` instead.
PRO_FEATURES: dict[str, str] = {
    # Built + gated in code:
    "compliance": "Compliance: RTBF, audit trails, custom PII governance (P1)",
    "encryption_at_rest": "Encryption-at-rest (SQLCipher/Fernet) + key mgmt (P2)",
    "multi_tenant": "Real storage-layer multi-tenant isolation (P4)",
    "advanced_p2p": "Advanced P2P: key-rotation, multi-node mesh (P6)",
    # Non-code commercial entitlements (support/release commitments, not a code
    # path — legitimately flag-only, granted by license, never a stub in code):
    "priority_support": "Priority support + SLA (V5)",
    "lts": "Long-term support (LTS) releases (V8)",
}

#: Planned Pro features — **not yet built, not advertised as available**. These
#: are roadmap items (see ROADMAP.md → "Enterprise Pro Roadmap"), kept OUT of the
#: enforceable ``PRO_FEATURES`` registry so nothing here can be sold or gated as a
#: present capability. When one is built to enterprise grade, MOVE its key into
#: ``PRO_FEATURES`` and add its ``assert_licensed`` gate in the same change.
PLANNED_PRO_FEATURES: dict[str, str] = {
    "advanced_auth": "RBAC / OIDC / per-user API keys / SSO (P3)",
    "scale_out": "Scale-out backend (Postgres/pgvector), HA (P5)",
    "team_analytics": "Team dashboard & analytics (V1)",
    "key_vault": "Managed key vault / HSM integration (V2)",
    "compliance_reporting": "Compliance reporting & exports (V3)",
    "sso_directory_sync": "SSO / directory sync — OIDC, SAML, SCIM (V4)",
    "backup_pitr": "Backup/restore orchestration & PITR (V6)",
    "ha_topology": "Horizontal read scaling & HA topology (V7)",
}

#: Wildcard that grants all Pro features (issued to full-license customers).
WILDCARD = "*"

#: Embedded QuantumindSSI license-signing public key (Ed25519, base64url, raw
#: 32 bytes). This is the PUBLIC half only; the PRIVATE signing key never lives
#: in this repo (it stays in an offline signing vault). Keys are minted with
#: ``scripts/issue_license.py`` and verified here fully offline (no phone-home).
_QUANTUMINDSSI_PUBLIC_KEY_B64: str = "eqwqRl8UYNfDvuiW_OD59xL8EybR2DnCSBscifPPTeY"


@dataclass(frozen=True)
class License:
    """A validated Pro license."""

    subject: str
    edition: str
    features: frozenset[str]
    seats: int = 0
    expires: date | None = None
    license_id: str = ""
    raw: str = field(default="", repr=False)

    @property
    def is_pro(self) -> bool:
        return self.edition == "pro"

    def grants(self, feature: str) -> bool:
        """Does this license entitle ``feature``?"""
        if not self.is_pro:
            return False
        if self.expired:
            return False
        return WILDCARD in self.features or feature in self.features

    @property
    def expired(self) -> bool:
        return self.expires is not None and self.expires < date.today()


#: The Community sentinel — no Pro entitlements at all.
COMMUNITY = License(subject="community", edition="community", features=frozenset())


def _b64url_decode(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _verify_signature(payload_bytes: bytes, signature: bytes) -> bool:
    """Verify an Ed25519 detached signature against the embedded public key.

    Fail-closed: any missing dependency / key / verification error → False.
    """
    if not _QUANTUMINDSSI_PUBLIC_KEY_B64:
        # No production public key embedded yet → cannot trust any Pro key.
        return False
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except Exception:
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(_b64url_decode(_QUANTUMINDSSI_PUBLIC_KEY_B64))
        pub.verify(signature, payload_bytes)
        return True
    except InvalidSignature:
        return False
    except Exception:
        return False


def parse_license_key(key: str) -> License:
    """Parse and cryptographically verify a license key.

    Raises:
        LicenseInvalidError: malformed, badly signed, or expired key.
    """
    key = key.strip()
    if not key.startswith("LUMENA-"):
        raise LicenseInvalidError("unrecognized key format")
    body = key[len("LUMENA-") :]
    if "." not in body:
        raise LicenseInvalidError("missing signature segment")
    payload_seg, sig_seg = body.split(".", 1)
    try:
        payload_bytes = _b64url_decode(payload_seg)
        signature = _b64url_decode(sig_seg)
    except (binascii.Error, ValueError) as exc:
        raise LicenseInvalidError("corrupt base64 encoding") from exc

    if not _verify_signature(payload_bytes, signature):
        raise LicenseInvalidError("signature verification failed")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LicenseInvalidError("payload is not valid JSON") from exc

    exp_raw = payload.get("exp")
    expires: date | None = None
    if exp_raw:
        try:
            expires = datetime.strptime(str(exp_raw), "%Y-%m-%d").date()
        except ValueError as exc:
            raise LicenseInvalidError("invalid 'exp' date") from exc

    lic = License(
        subject=str(payload.get("sub", "unknown")),
        edition=str(payload.get("edition", "community")),
        features=frozenset(payload.get("features", [])),
        seats=int(payload.get("seats", 0) or 0),
        expires=expires,
        license_id=str(payload.get("id", "")),
        raw=key,
    )
    if lic.expired:
        raise LicenseInvalidError(f"license expired on {lic.expires}")
    return lic


@lru_cache(maxsize=8)
def _load_license_cached(key: str | None) -> License:
    if not key:
        return COMMUNITY
    try:
        return parse_license_key(key)
    except LicenseInvalidError:
        # Fail-closed: an invalid key means Community, not a crash on import.
        # The explicit assert_licensed() path re-raises so users get a clear
        # error only when they actually touch a Pro feature.
        return COMMUNITY


def load_license(config: LumenaConfig | None = None) -> License:
    """Resolve the active license from config/env.

    Precedence: explicit ``config.license_key`` → COMMUNITY.
    """
    key: str | None = None
    if config is not None:
        key = getattr(config, "license_key", None)
    return _load_license_cached(key)


def current_edition(config: LumenaConfig | None = None) -> str:
    return "pro" if load_license(config).is_pro else "community"


def is_licensed(feature: str, config: LumenaConfig | None = None) -> bool:
    """Non-raising check: is ``feature`` entitled under the active license?"""
    return load_license(config).grants(feature)


def assert_licensed(feature: str, config: LumenaConfig | None = None) -> None:
    """Gate a Pro feature. Mirrors ``LumenaConfig.assert_sovereign``.

    Call this at the entry of any Pro-only code path.

    Raises:
        LicenseRequiredError: no valid license entitles ``feature``.
    """
    if feature in PLANNED_PRO_FEATURES:
        # A planned-but-unbuilt feature must never be silently gated as if it
        # exists (AGENTS.md Rule 2/3). If code reaches here, the capability was
        # wired before it was implemented — surface that as a programming error.
        raise ValueError(
            f"Pro feature {feature!r} is planned but not yet built "
            f"(see PLANNED_PRO_FEATURES / ROADMAP.md); do not gate it as available."
        )
    if feature not in PRO_FEATURES and feature != WILDCARD:
        # Guard against typos: an unknown feature key is a programming error,
        # not a licensing decision. Surface it loudly in that case.
        raise ValueError(f"Unknown Pro feature key: {feature!r}")
    lic = load_license(config)
    if not lic.grants(feature):
        raise LicenseRequiredError(PRO_FEATURES.get(feature, feature))


def clear_license_cache() -> None:
    """Reset cached license resolution (useful in tests / after key rotation)."""
    _load_license_cached.cache_clear()
