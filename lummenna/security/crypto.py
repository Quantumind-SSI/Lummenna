# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-QuantumindSSI-Commercial
# Copyright (c) 2025-2026 QuantumindSSI. All rights reserved.
# NOTE: Encryption-at-rest + key management built on these primitives are gated
# as Lummenna Pro (see COMMERCIAL-LICENSE.md, P2). Primitives themselves are Community.
"""S1: Encryption primitives for Lummenna.

Provides:
  - Fernet field-level encryption (fallback when SQLCipher is unavailable).
  - SQLCipher passphrase derivation with persisted salt.
  - P2P transport encryption (AES-256-GCM) + HMAC-SHA256 frame signing.
  - The replay-nonce store interface used by frame verification, plus the
    process-local (in-memory) implementation. The durable SQLite-backed
    implementation lives in ``lummenna.p2p.replay`` so that this module, which
    ``lummenna.data.schema`` imports, never has to import the storage layer back.
"""

from __future__ import annotations

import base64
import hashlib
import heapq
import os
import struct
import time
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class ReplayStoreError(RuntimeError):
    """A replay-nonce store could not answer a replay question.

    Raised instead of returning a verdict when the backing store is unreachable
    or refuses a write. Verification treats this as fatal for the frame: an
    unanswerable replay check must reject, never accept, because accepting would
    silently downgrade the transport to timestamp-window-only protection at the
    exact moment the operator has least visibility.

    Deliberately not a ``ValueError``: callers distinguish "this frame is bad"
    (``ValueError``) from "this node cannot currently police replays"
    (``ReplayStoreError``), because the two need different operational responses.
    """


class ReplayNonceStore(Protocol):
    """A set of recently-seen frame nonces with a bounded retention horizon.

    Implementations must make :meth:`record_if_unseen` atomic with respect to
    concurrent callers: two callers presenting the same nonce must not both
    receive ``True``, or a replayed frame is accepted by both.
    """

    def record_if_unseen(self, nonce: bytes, expires_at: int) -> bool:
        """Record *nonce* and report whether it was previously unseen.

        Args:
            nonce: Opaque nonce bytes identifying one frame.
            expires_at: Unix timestamp after which the nonce may be discarded,
                because the frame carrying it can no longer be accepted anyway.

        Returns:
            True if the nonce was not present (the frame is fresh), False if it
            was already recorded (the frame is a replay).

        Raises:
            ReplayStoreError: If the store cannot service the request.
        """
        ...

    def prune(self, now: int) -> int:
        """Drop every nonce whose ``expires_at`` is at or before *now*.

        Returns:
            The number of nonces dropped.

        Raises:
            ReplayStoreError: If the store cannot service the request.
        """
        ...


#: Hard ceiling on the process-local nonce set. Retention is normally driven by
#: expiry (see :class:`InMemoryNonceStore`), so this only binds when frames
#: arrive faster than the replay window can retire them — i.e. under flood.
DEFAULT_MAX_NONCES: int = 100_000


class InMemoryNonceStore:
    """Process-local replay-nonce store. Fast, and lost on restart.

    This is the Community default and satisfies the threat model Beam is scoped
    to: a trusted LAN where an attacker capturing a frame must replay it inside
    the signing window. It does **not** survive a restart, so a frame captured
    seconds before a crash can be replayed once after it, for the remainder of
    its window. ``lummenna.p2p.replay.SqliteNonceStore`` closes that gap.

    Eviction is expiry-first: nonces are retired once the frames that carry them
    have aged out of the replay window, which is exactly when they stop being
    useful to an attacker. Only if that leaves the set above ``max_entries`` are
    live nonces dropped, nearest-expiry first.
    """

    def __init__(self, max_entries: int = DEFAULT_MAX_NONCES):
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        self._max_entries = max_entries
        self._expiry: dict[bytes, int] = {}
        # Min-heap of (expires_at, nonce) used to find eviction candidates
        # without sorting the whole set. It may hold stale entries for nonces
        # already removed; those are skipped when popped.
        self._heap: list[tuple[int, bytes]] = []

    def __len__(self) -> int:
        return len(self._expiry)

    def record_if_unseen(self, nonce: bytes, expires_at: int) -> bool:
        if nonce in self._expiry:
            return False
        self._expiry[nonce] = expires_at
        heapq.heappush(self._heap, (expires_at, nonce))
        self._enforce_capacity()
        return True

    def prune(self, now: int) -> int:
        dropped = 0
        while self._heap and self._heap[0][0] <= now:
            _expires_at, nonce = heapq.heappop(self._heap)
            # Skip heap entries whose nonce was already evicted, and entries
            # that are stale duplicates of a still-live nonce.
            if self._expiry.get(nonce) == _expires_at:
                del self._expiry[nonce]
                dropped += 1
        return dropped

    def _enforce_capacity(self) -> None:
        """Drop nearest-expiry nonces until the set is back within its ceiling."""
        while len(self._expiry) > self._max_entries and self._heap:
            expires_at, nonce = heapq.heappop(self._heap)
            if self._expiry.get(nonce) == expires_at:
                del self._expiry[nonce]


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


class P2PSign:
    """HMAC-SHA256 frame signing with embedded timestamp and replay-window protection.

    Wire format (prepended to encrypted or plaintext payload):
        timestamp_be64 (8 bytes) || signature (32 bytes) || original_payload

    Replay protection has two independent layers. The timestamp must fall inside
    ``replay_window_seconds`` of local time, and the frame's nonce must not have
    been seen before. The nonce is derived from the timestamp and signature, so
    it is bound to the signing key: frames signed under different keys can never
    collide, which is why a single store is safe to share across a key rotation.

    Where "seen before" is remembered is the caller's choice. The default is a
    process-local set (:class:`InMemoryNonceStore`); pass a durable store such as
    ``lummenna.p2p.replay.SqliteNonceStore`` to carry replay protection across
    restarts.
    """

    def __init__(
        self,
        key: str | None = None,
        replay_window_seconds: int = 30,
        nonce_store: ReplayNonceStore | None = None,
    ):
        if replay_window_seconds < 1:
            raise ValueError("replay_window_seconds must be >= 1")
        self._key = hashlib.sha256(key.encode("utf-8")).digest() if key else None
        self._replay_window = replay_window_seconds
        self._nonce_store: ReplayNonceStore = (
            nonce_store if nonce_store is not None else InMemoryNonceStore()
        )

    @property
    def enabled(self) -> bool:
        return self._key is not None

    @property
    def replay_window_seconds(self) -> int:
        """Seconds of clock skew tolerated between a frame's timestamp and now."""
        return self._replay_window

    @property
    def nonce_store(self) -> ReplayNonceStore:
        """The store backing this signer's replay check."""
        return self._nonce_store

    def rekey(self, key: str) -> None:
        """Replace the signing key, keeping the nonce store attached.

        Rebuilding the signer instead would detach the store and reopen the
        replay window for frames still inside it. Keeping it is sound because a
        nonce is derived from the frame's HMAC and is therefore already bound to
        the key that produced it: nonces recorded under the old key can never
        collide with nonces produced under the new one.
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

        Raises:
            ValueError: On a malformed, tampered, expired, or replayed frame.
            ReplayStoreError: If the replay-nonce store cannot be consulted. The
                frame is neither accepted nor recorded; the caller must drop it.
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

        # The nonce only needs to outlive the window in which the frame carrying
        # it could still be accepted. Past ts + window the timestamp check above
        # rejects the frame unconditionally, so retaining the nonce beyond that
        # point buys no additional protection.
        nonce = hashlib.sha256(ts_bytes + sig).digest()
        if not self._nonce_store.record_if_unseen(nonce, ts + self._replay_window):
            raise ValueError("Replay detected")
        return payload
