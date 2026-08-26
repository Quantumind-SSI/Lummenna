"""Tests for SQLCipher encryption-at-rest integration.

encryption-at-rest is a Pro (P2) feature gated at ``get_connection``, so these
tests supply a valid ephemeral Pro license. They are skipped only when the
optional ``sqlcipher3`` driver is not installed in the environment; the driver
IS a declared dependency (pyproject ``sqlcipher3``), so in CI these run for real.
"""

import base64
import importlib.util
import json

import pytest

from lummenna import licensing
from lummenna.config import LumenaConfig
from lummenna.data.schema import get_connection, get_encryption
from lummenna.force.contextual.embed import MockEmbedder
from lummenna.force.mnemonic.store import store_memory
from lummenna.search import SearchPipeline
from lummenna.security.crypto import derive_sqlcipher_passphrase

# Skip the whole module when the SQLCipher driver is unavailable rather than
# fail: the capability itself is exercised only when sqlcipher3 is importable.
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("sqlcipher3") is None,
    reason="sqlcipher3 driver not installed in this environment",
)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


@pytest.fixture
def pro_license(monkeypatch):
    """Embed an ephemeral signing key and return a valid Pro license key.

    encryption-at-rest is gated by ``assert_licensed("encryption_at_rest")`` in
    ``get_connection``; without a Pro key the connection would raise
    ``LicenseRequiredError`` before SQLCipher is ever touched.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(licensing, "_QUANTUMINDSSI_PUBLIC_KEY_B64", _b64(pub))
    payload = json.dumps(
        {"sub": "t", "edition": "pro", "features": ["*"], "iss": "QuantumindSSI", "id": "x"},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    key = f"LUMENA-{_b64(payload)}.{_b64(priv.sign(payload))}"
    licensing.clear_license_cache()
    yield key
    licensing.clear_license_cache()


@pytest.fixture
def sqlcipher_config(tmp_path, pro_license):
    return LumenaConfig(
        store_path=tmp_path / ".lumena" / "store",
        model_path=tmp_path / ".lumena" / "models",
        vector_index="sqlite-vec",
        device="generic",
        encryption_key="test-sqlcipher-key-12345",
        database_encryption_mode="sqlcipher",
        license_key=pro_license,
    )


class TestSQLCipherIntegration:
    def test_sqlcipher_connection_opens(self, sqlcipher_config):
        conn = get_connection(sqlcipher_config)
        try:
            conn.execute("INSERT INTO room(name) VALUES ('test_room')")
            conn.commit()
            row = conn.execute("SELECT name FROM room WHERE name = 'test_room'").fetchone()
            assert row is not None
        finally:
            conn.close()

    def test_db_file_is_encrypted(self, sqlcipher_config):
        conn = get_connection(sqlcipher_config)
        conn.execute("INSERT INTO room(name) VALUES ('secret')")
        conn.commit()
        conn.close()

        db_path = sqlcipher_config.db_path
        raw = db_path.read_bytes()
        # Encrypted SQLite files must NOT contain the plaintext room name ...
        assert b"secret" not in raw
        # ... and must not carry the standard plaintext SQLite header.
        assert raw[:16] != b"SQLite format 3\x00"

    def test_salt_file_created(self, sqlcipher_config):
        conn = get_connection(sqlcipher_config)
        conn.close()
        salt_path = sqlcipher_config.store_path / ".lumena_salt"
        assert salt_path.exists()
        assert salt_path.stat().st_size == 16

    def test_derived_passphrase_deterministic(self, sqlcipher_config):
        db_path = sqlcipher_config.store_path
        db_path.mkdir(parents=True, exist_ok=True)
        p1 = derive_sqlcipher_passphrase("mykey", db_path)
        p2 = derive_sqlcipher_passphrase("mykey", db_path)
        assert p1 == p2
        assert len(p1) == 64  # 32 bytes hex-encoded

    def test_get_encryption_disabled_under_sqlcipher(self, sqlcipher_config):
        # Page-level SQLCipher encrypts the whole file, so field-level Fernet
        # must be OFF (double-encrypting content would break dedup/search).
        enc = get_encryption(sqlcipher_config)
        assert not enc.enabled

    def test_store_and_retrieve_with_sqlcipher(self, sqlcipher_config):
        conn = get_connection(sqlcipher_config)
        try:
            embedder = MockEmbedder(dims=sqlcipher_config.embedding_dims)
            chunk_id = store_memory(
                conn,
                content="SQLCipher protected memory",
                room_name="secure",
                embedding=embedder.encode_single("SQLCipher protected memory"),
                config=sqlcipher_config,
            )
            pipeline = SearchPipeline(conn, sqlcipher_config, embedder=embedder)
            results = pipeline.execute("protected memory", k=5)
            assert any(r.chunk_id == chunk_id for r in results)
        finally:
            conn.close()

    def test_encrypted_db_unreadable_as_plain_sqlite(self, sqlcipher_config):
        """A SQLCipher DB must not open as a plain sqlite3 database.

        Proves the on-disk file genuinely requires the key: opening it with the
        stdlib sqlite3 driver (no key) fails to read the schema.
        """
        import sqlite3

        conn = get_connection(sqlcipher_config)
        conn.execute("INSERT INTO room(name) VALUES ('locked')")
        conn.commit()
        conn.close()

        plain = sqlite3.connect(str(sqlcipher_config.db_path))
        try:
            with pytest.raises(sqlite3.DatabaseError):
                plain.execute("SELECT name FROM room").fetchall()
        finally:
            plain.close()

    def test_plaintext_fallback_when_sqlcipher_unavailable(
        self, tmp_path, monkeypatch, pro_license
    ):
        monkeypatch.setattr("lummenna.data.schema._HAS_SQLCIPHER", False)
        config = LumenaConfig(
            store_path=tmp_path / ".lumena" / "store",
            model_path=tmp_path / ".lumena" / "models",
            vector_index="sqlite-vec",
            device="generic",
            encryption_key="key",
            database_encryption_mode="sqlcipher",
            license_key=pro_license,
        )
        with pytest.raises(RuntimeError, match="sqlcipher3 is not installed"):
            get_connection(config)

    def test_sqlcipher_blocked_without_pro_license(self, tmp_path):
        """Community (no license) must not be able to enable encryption-at-rest."""
        from lummenna.brand.errors import LicenseRequiredError

        licensing.clear_license_cache()
        config = LumenaConfig(
            store_path=tmp_path / ".lumena" / "store",
            model_path=tmp_path / ".lumena" / "models",
            vector_index="sqlite-vec",
            device="generic",
            encryption_key="test-sqlcipher-key-12345",
            database_encryption_mode="sqlcipher",
        )
        with pytest.raises(LicenseRequiredError):
            get_connection(config)
