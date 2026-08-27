"""Tests for the default-deny network egress guard in ``tests/conftest.py``.

The guard underwrites Lummenna's central claim: memory never leaves the
machine. A guard that silently failed open would be worse than no guard,
because it would manufacture false confidence. These tests prove it fires on
egress, stays out of the way on loopback, and can be waived deliberately.
"""

import socket

import pytest

from tests.conftest import NetworkEgressBlocked, _is_loopback_address

# TEST-NET-1 (RFC 5737). Reserved for documentation and guaranteed never to be
# routed, so a test that reaches the OS with this address cannot touch a real
# host even if the guard were removed.
UNROUTABLE = ("192.0.2.1", 9)


class TestLoopbackClassification:
    """``_is_loopback_address`` decides what counts as leaving the machine."""

    @pytest.mark.parametrize(
        "address",
        [
            ("127.0.0.1", 80),
            ("127.53.0.1", 80),  # all of 127.0.0.0/8 is loopback
            (b"127.0.0.1", 80),  # bytes host
            ("::1", 80, 0, 0),  # IPv6 loopback, 4-tuple
            ("localhost", 80),
            ("LOCALHOST", 80),  # case-insensitive
            ("", 80),  # empty host means local
            "/tmp/lummenna.sock",  # AF_UNIX path
            b"/tmp/lummenna.sock",  # AF_UNIX bytes path
            (),  # degenerate empty tuple
        ],
    )
    def test_host_local_addresses_are_permitted(self, address):
        assert _is_loopback_address(address) is True

    @pytest.mark.parametrize(
        "address",
        [
            ("8.8.8.8", 53),
            ("192.0.2.1", 9),
            ("2001:4860:4860::8888", 53, 0, 0),
            ("fe80::1%lo0", 80, 0, 1),  # link-local is not loopback
            ("example.com", 80),
            ("lummenna.com", 443),
            (None, 80),  # unusable host must not fail open
        ],
    )
    def test_off_host_addresses_are_denied(self, address):
        assert _is_loopback_address(address) is False


class TestGuardEnforcement:
    """The autouse fixture is active in this test by default."""

    def test_connect_to_off_host_address_raises(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(NetworkEgressBlocked) as excinfo:
            sock.connect(UNROUTABLE)
        sock.close()
        message = str(excinfo.value)
        assert "192.0.2.1" in message
        assert "sovereign" in message

    def test_connect_ex_to_off_host_address_raises(self):
        """connect_ex returns errnos rather than raising, so it is a bypass."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(NetworkEgressBlocked):
            sock.connect_ex(UNROUTABLE)
        sock.close()

    def test_create_connection_is_covered(self):
        """The stdlib helper routes through connect, so it inherits the guard."""
        with pytest.raises(NetworkEgressBlocked):
            socket.create_connection(UNROUTABLE, timeout=0.01)

    def test_error_names_the_offending_test(self):
        """The message must identify the test so a CI failure is actionable."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(NetworkEgressBlocked) as excinfo:
            sock.connect(UNROUTABLE)
        sock.close()
        assert "test_error_names_the_offending_test" in str(excinfo.value)

    def test_loopback_connections_still_work(self):
        """Egress is the boundary, not ingress: the local API must stay testable."""
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            client = socket.create_connection(("127.0.0.1", port), timeout=2.0)
            accepted, _ = listener.accept()
            client.sendall(b"sovereign")
            assert accepted.recv(16) == b"sovereign"
            accepted.close()
            client.close()
        finally:
            listener.close()


class TestGuardWaiver:
    @pytest.mark.allow_network
    def test_marker_lifts_the_guard(self):
        """A marked test reaches the real socket layer instead of the guard.

        The address is unroutable, so the OS call fails locally without any
        packet reaching a real host. What matters is *which* error surfaces:
        an OSError proves the guard stepped aside.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.01)
        try:
            sock.connect_ex(UNROUTABLE)
        except NetworkEgressBlocked:
            pytest.fail("allow_network marker did not lift the egress guard")
        except OSError:
            pass  # expected: unroutable address, resolved by the real stack
        finally:
            sock.close()

    def test_guard_is_restored_after_a_waived_test(self):
        """The waiver must not leak into the next test in the same module."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with pytest.raises(NetworkEgressBlocked):
            sock.connect(UNROUTABLE)
        sock.close()
