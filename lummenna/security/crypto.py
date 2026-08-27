# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-QuantumindSSI-Commercial
# Copyright (c) 2025-2026 QuantumindSSI. All rights reserved.
# NOTE: Encryption-at-rest + key management built on these primitives are gated
# as Lummenna Pro (see COMMERCIAL-LICENSE.md, P2). Primitives themselves are Community.
"""S1: Encryption primitives for Lummenna.

Provides:
  - Fernet field-level encryption (fallback when SQLCipher is unavailable).
  - SQLCipher passphrase derivation with persisted salt.
  - P2P transport encryption (AES-256-GCM) + HMAC-SHA256 frame signing.
"""

from __future__ import annotations

import base64
import hashlib
import heapq
import os
import struct
import time
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def derive_sqlcipher_passphrase(key: str, store_path: Path, iterations: int = 256_000) -> str:
    """Derive a high-entropy hex passphrase for SQLCipher PRAGMA key.

    Persists a random 16-byte salt in *store_path/.lummenna_salt* so the
    same user key always yields the same derived passphrase across restarts.
    """
    salt_path = store_path / ".lummenna_salt"
    if salt_path.exists():
        salt = salt_path.read_bytes()
    else:
        salt = os.urandom(16)
        store_path.mkdir(parents=True, exist_ok=True)
        salt_path.write_bytes(salt)
        os.chmod(salt_path, 0o600)

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    derived = kdf.derive(key.encode("utf-8"))
    return str(derived.hex())


class FernetEncryption:
    """Field-level encryption using Fernet (AES-128-CBC + HMAC-SHA256)."""

    def __init__(self, key: str | None = None):
        """Initialise with a user-supplied key.

        Args:
            key: Any string; it is hashed to a 32-byte Fernet key.
        """
        self._fernet: Fernet | None
        if key:
            # Derive a URL-safe base64-encoded 32-byte key from arbitrary input
            digest = hashlib.sha256(key.encode("utf-8")).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(digest))
        else:
            self._fernet = None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> bytes:
        """Encrypt *plaintext* and return URL-safe base64 bytes."""
        if self._fernet is None:
            raise RuntimeError("Encryption not initialised with a key")
        return bytes(self._fernet.encrypt(plaintext.encode("utf-8")))

    def decrypt(self, token: bytes | str) -> str:
        """Decrypt *token* and return the plaintext string."""
        if self._fernet is None:
            raise RuntimeError("Encryption not initialised with a key")
        if isinstance(token, str):
            token = token.encode("utf-8")
        return str(self._fernet.decrypt(token).decode("utf-8"))


class P2PCrypto:
    """AES-256-GCM transport encryption for Beam P2P packets.

    Each encrypted payload is: nonce (12 bytes) || ciphertext || tag (16 bytes).
    The framing layer handles length-prefixing of this combined blob.
    """

    def __init__(self, key: str | None = None):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        self._aesgcm: AESGCM | None
        if key:
            digest = hashlib.sha256(key.encode("utf-8")).digest()
            self._aesgcm = AESGCM(digest)
        else:
            self._aesgcm = None

    @property
    def enabled(self) -> bool:
        return self._aesgcm is not None

    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt *plaintext* and return nonce+ciphertext+tag."""
        if self._aesgcm is None:
            raise RuntimeError("P2P encryption not initialised with a key")
        nonce = os.urandom(12)
        return bytes(nonce + self._aesgcm.encrypt(nonce, plaintext, None))

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Decrypt *ciphertext* (nonce+ciphertext+tag) and plaintext."""
        if self._aesgcm is None:
            raise RuntimeError("P2P encryption not initialised with a key")
        nonce, ct = ciphertext[:12], ciphertext[12:]
        return bytes(self._aesgcm.decrypt(nonce, ct, None))


#: Ceiling on the seen-nonce set. Expiry normally retires nonces long before this
#: binds; it only matters under flood, when frames arrive faster than the replay
#: window can retire them.
MAX_SEEN_NONCES: int = 10_000


class P2PSign:
    """HMAC-SHA256 frame signing with embedded timestamp and replay-window protection.

    Wire format (prepended to encrypted or plaintext payload):
        timestamp_be64 (8 bytes) || signature (32 bytes) || original_payload

    A frame is accepted only if its timestamp is within ``replay_window_seconds``
    of local time *and* its nonce has not been seen before. The nonce is derived
    from the timestamp and signature, so it is bound to the signing key: frames
    signed under different keys can never collide.

    The seen-nonce set is process-local and is lost when the process exits.
    """

    def __init__(
        self,
        key: str | None = None,
        replay_window_seconds: int = 30,
        max_seen_nonces: int = MAX_SEEN_NONCES,
    ):
        if replay_window_seconds < 1:
            raise ValueError("replay_window_seconds must be >= 1")
        if max_seen_nonces < 1:
            raise ValueError("max_seen_nonces must be >= 1")
        self._key = hashlib.sha256(key.encode("utf-8")).digest() if key else None
        self._replay_window = replay_window_seconds
        self._max_seen_nonces = max_seen_nonces
        self._seen_expiry: dict[bytes, int] = {}
        # Min-heap of (expires_at, nonce), so retiring nonces in expiry order
        # costs a pop rather than a sort of the whole set. It may hold entries
        # for nonces already removed; those are skipped when popped.
        self._seen_heap: list[tuple[int, bytes]] = []

    @property
    def enabled(self) -> bool:
        return self._key is not None

    @property
    def replay_window_seconds(self) -> int:
        """Seconds of clock skew tolerated between a frame's timestamp and now."""
        return self._replay_window

    def rekey(self, key: str) -> None:
        """Replace the signing key, keeping the seen-nonce set intact.

        Rebuilding the signer instead would drop every recorded nonce and reopen
        the replay window for frames still inside it. Carrying the set across is
        sound because a nonce is derived from the frame's HMAC and is therefore
        already bound to the key that produced it: nonces recorded under the old
        key can never collide with nonces produced under the new one.
        """
        if not key:
            raise ValueError("rekey requires a non-empty key")
        self._key = hashlib.sha256(key.encode("utf-8")).digest()

    def sign(self, payload: bytes) -> bytes:
        """Return timestamp || HMAC || payload."""
        if self._key is None:
            raise RuntimeError("P2P signing not initialised with a key")
        ts = struct.pack(">Q", int(time.time()))
        mac = hmac.HMAC(self._key, hashes.SHA256())
        mac.update(ts + payload)
        return bytes(ts + mac.finalize() + payload)

    def verify(self, framed: bytes) -> bytes:
        """Verify HMAC and timestamp; return original payload.

        Raises ValueError on tampering, expiry, or replay.
        """
        if self._key is None:
            raise RuntimeError("P2P signing not initialised with a key")
        if len(framed) < 40:
            raise ValueError("Frame too short for signed payload")
        ts_bytes, sig, payload = framed[:8], framed[8:40], framed[40:]
        ts = struct.unpack(">Q", ts_bytes)[0]
        now = int(time.time())
        if abs(now - ts) > self._replay_window:
            raise ValueError("Frame outside replay window")

        mac = hmac.HMAC(self._key, hashes.SHA256())
        mac.update(ts_bytes + payload)
        try:
            mac.verify(sig)
        except Exception as exc:
            raise ValueError("Frame signature verification failed") from exc

        # A nonce only needs to outlive the window in which its frame could still
        # be accepted. Past ts + window the timestamp check above rejects the
        # frame unconditionally, so retaining the nonce buys no extra protection.
        nonce = hashlib.sha256(ts_bytes + sig).digest()
        if not self._record_nonce(nonce, ts + self._replay_window):
            raise ValueError("Replay detected")
        return payload

    def _record_nonce(self, nonce: bytes, expires_at: int) -> bool:
        """Record *nonce* and report whether it was previously unseen."""
        self._expire_nonces(int(time.time()))
        if nonce in self._seen_expiry:
            return False
        self._seen_expiry[nonce] = expires_at
        heapq.heappush(self._seen_heap, (expires_at, nonce))
        # Only reachable when frames arrive faster than the window retires them.
        # Evicting nearest-expiry first sacrifices the nonces with the least
        # protective value left.
        while len(self._seen_expiry) > self._max_seen_nonces and self._seen_heap:
            self._drop_heap_head()
        return True

    def _expire_nonces(self, now: int) -> int:
        """Drop every nonce whose frame can no longer pass the timestamp check."""
        dropped = 0
        while self._seen_heap and self._seen_heap[0][0] <= now:
            dropped += self._drop_heap_head()
        return dropped

    def _drop_heap_head(self) -> int:
        """Pop the nearest-expiry heap entry; return 1 if it retired a live nonce."""
        expires_at, nonce = heapq.heappop(self._seen_heap)
        # Skip entries whose nonce was already retired by another path.
        if self._seen_expiry.get(nonce) == expires_at:
            del self._seen_expiry[nonce]
            return 1
        return 0
