"""D14: In-Memory Test Fixtures & Harness.

Input wire: pytest, SQLite :memory:, mock embedding model
Output wire: All test files
"""

import ipaddress
import socket
import sqlite3

import numpy as np
import pytest

from lummenna.config import LummennaConfig
from lummenna.data.schema import init_db

# Hostnames that resolve to the loopback interface. A connection to any of
# these never leaves the machine, so it does not cross the sovereign boundary
# (DEPLOYMENT.md: inbound/loopback traffic is explicitly in scope).
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", ""})


class NetworkEgressBlocked(RuntimeError):
    """Raised when a test attempts a socket connection off the local machine.

    Deliberately not ``SovereignViolationError``: a test asserting that
    production code raises the real error must not be able to pass because the
    harness raised it instead.
    """


def _is_loopback_address(address: object) -> bool:
    """Return True if ``address`` refers to the local machine.

    Args:
        address: The value passed to ``socket.socket.connect``. For AF_INET
            this is ``(host, port)``; for AF_INET6 ``(host, port, flow, scope)``;
            for AF_UNIX a path string or bytes.

    Returns:
        True if the connection cannot leave the host, False otherwise.
    """
    # AF_UNIX and any other non-tuple address family are host-local by
    # construction (filesystem sockets, netlink, bluetooth).
    if not isinstance(address, tuple) or not address:
        return True

    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("utf-8", "replace")
    if not isinstance(host, str):
        return False

    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        # Not an IP literal, so it is a hostname that connect() will resolve.
        return host.lower() in _LOOPBACK_HOSTNAMES


@pytest.fixture(autouse=True)
def block_network_egress(request, monkeypatch):
    """Fail any test that opens a socket to a non-loopback address.

    Lummenna's central claim is that memory never leaves the machine
    (``LummennaConfig.sovereign``, enforced by ``assert_sovereign``). That
    guarantee is only as strong as the guarantee that no new code path opens a
    socket, and nothing in the suite checked for that. This fixture is
    default-deny: every test is guarded unless it is explicitly marked.

    Loopback is permitted because the sovereign boundary is egress, not
    ingress: the local API server and its test client are in scope.

    Opt out for a test that genuinely needs off-host traffic::

        @pytest.mark.allow_network
        def test_p2p_share_over_lan(): ...
    """
    if request.node.get_closest_marker("allow_network"):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _guard(operation, wrapped):
        def guarded(self, address, *args, **kwargs):
            if not _is_loopback_address(address):
                raise NetworkEgressBlocked(
                    f"{request.node.nodeid} attempted socket.{operation}({address!r}), "
                    f"which leaves the local machine. Lummenna is sovereign by "
                    f"default: route this through an explicit "
                    f"config.assert_sovereign(...) gate, or mark the test with "
                    f"@pytest.mark.allow_network if off-host traffic is the "
                    f"behaviour under test."
                )
            return wrapped(self, address, *args, **kwargs)

        return guarded

    monkeypatch.setattr(socket.socket, "connect", _guard("connect", real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", _guard("connect_ex", real_connect_ex))


@pytest.fixture
def memory_db():
    """Yield an in-memory SQLite connection with the Lummenna schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture
def mock_embedder():
    """Return a deterministic mock embedder for tests."""

    class MockEmbedder:
        dims = 384

        def encode_single(self, text: str) -> np.ndarray:
            # Deterministic pseudo-embedding from text hash
            h = hash(text) % (2**31)
            np.random.seed(h)
            vec = np.random.randn(self.dims).astype(np.float32)
            return vec / np.linalg.norm(vec)

    return MockEmbedder()


@pytest.fixture
def test_config():
    """Return a LummennaConfig tuned for testing."""
    return LummennaConfig(
        vector_index="sqlite-vec",
        device="generic",
        store_path="/tmp/.lummenna-test",
    )
