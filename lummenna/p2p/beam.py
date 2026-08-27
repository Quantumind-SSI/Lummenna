# SPDX-License-Identifier: AGPL-3.0-or-later OR LicenseRef-QuantumindSSI-Commercial
# Copyright (c) 2025-2026 QuantumindSSI. All rights reserved.
# NOTE: Advanced P2P — currently the live encryption/signing key-rotation
# capability (BeamNode.rotate_key) — is gated as Lummenna Pro (see
# COMMERCIAL-LICENSE.md, P6). Basic single-key LAN sharing (constructing a
# BeamNode and running start()/share_room()) is Community.
#
# NOT YET BUILT (roadmap, see ROADMAP.md, must not be advertised as available):
#   * multi-node mesh routing — discovery + transport here are flat mDNS with
#     direct 1-hop TCP sends; there is no multi-hop/relay/gossip topology.
#   * persistent replay store — the replay-nonce set held by P2PSign is in-memory
#     only and resets on restart; it is not durable across restarts.
"""C10: P2P Memory Sharing (Beam Protocol).

Household-only, LAN-scoped memory sharing with mandatory encryption,
HMAC signing, replay protection, and TTL enforcement.

SECURITY POSTURE:
  - Encryption: AES-256-GCM (via P2PCrypto).
  - Integrity / Authenticity: HMAC-SHA256 with a 30-second replay window and an
    in-memory replay-nonce set (per-process; not persisted across restarts). A
    nonce is retained until its frame falls out of the replay window, past which
    the timestamp check rejects the frame regardless.
  - TTL: packets older than their ttl_hours are dropped at reception.
  - Topology: flat mDNS peer discovery with direct 1-hop TCP delivery (no mesh).
  - Plaintext fallback: REMOVED. Enabling Beam without a p2p_encryption_key
    raises a SecurityError. To override (e.g. for local testing), set
    LUMMENNA_P2P_INSECURE=1 in the environment.
"""

import asyncio
import logging
import os
import socket
import struct
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import msgspec

from lummenna.config import LummennaConfig
from lummenna.data.schema import get_connection
from lummenna.force.mnemonic.store import store_memory
from lummenna.security.crypto import P2PCrypto, P2PSign

logger = logging.getLogger(__name__)

# Availability flags
availability: dict[str, bool] = {}

if TYPE_CHECKING:
    # Import solely for static typing; at runtime these may be unavailable.
    from zeroconf import ServiceInfo
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf
else:
    ServiceInfo = None
    AsyncServiceBrowser = None
    AsyncZeroconf = None

try:
    from zeroconf import ServiceInfo
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

    availability["zeroconf"] = True
except Exception:
    availability["zeroconf"] = False


def _get_default_ip() -> str:
    """Best-effort local IPv4 detection."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        try:
            s.connect(("10.254.254.254", 1))
            ip = str(s.getsockname()[0])
        except Exception:
            ip = "127.0.0.1"
        finally:
            s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def encode_frame(
    packet: dict, crypto: P2PCrypto | None = None, signer: P2PSign | None = None
) -> bytes:
    """Serialize *packet* with optional encryption + HMAC signing and 4-byte length prefix."""
    payload: bytes = msgspec.json.encode(packet)
    if crypto is not None and crypto.enabled:
        payload = crypto.encrypt(payload)
    if signer is not None and signer.enabled:
        payload = signer.sign(payload)
    return struct.pack(">I", len(payload)) + payload


async def decode_frame(
    reader: asyncio.StreamReader,
    crypto: P2PCrypto | None = None,
    signer: P2PSign | None = None,
) -> dict | None:
    """Read a length-prefixed frame from *reader*, verify signature, decrypt, and decode JSON."""
    length_bytes = await reader.readexactly(4)
    length = struct.unpack(">I", length_bytes)[0]
    payload = await reader.readexactly(length)
    if signer is not None and signer.enabled:
        payload = signer.verify(payload)
    if crypto is not None and crypto.enabled:
        payload = crypto.decrypt(payload)
    decoded: dict | None = msgspec.json.decode(payload)
    return decoded


class BeamNode:
    """Hardened household P2P memory sharing node."""

    def __init__(self, config: LummennaConfig):
        if not availability["zeroconf"]:
            raise RuntimeError(
                "zeroconf is required for the P2P beam protocol but is not installed. "
                "Install it with: pip install 'zeroconf>=0.132'"
            )
        self.config = config
        self.service_type = "_lummenna-beam._tcp.local."
        self.peers: dict[str, tuple[str, int]] = {}
        self.device_name = getattr(config, "device_name", socket.gethostname())
        self.local_ip = getattr(config, "local_ip", _get_default_ip())
        self.beam_port = getattr(config, "beam_port", 8847)
        self._azc: AsyncZeroconf | None = None
        self._browser: AsyncServiceBrowser | None = None
        self._server: asyncio.Server | None = None

        p2p_key = getattr(config, "p2p_encryption_key", None)
        if not p2p_key:
            if os.environ.get("LUMMENNA_P2P_INSECURE") == "1":
                logger.critical(
                    "beam_running_in_plaintext_insecure_mode — LUMMENNA_P2P_INSECURE=1 is set. "
                    "This is NOT safe for production."
                )
            else:
                raise RuntimeError(
                    "Beam P2P requires a p2p_encryption_key. "
                    "Set LUMMENNA_P2P_ENCRYPTION_KEY or export LUMMENNA_P2P_INSECURE=1 to override (not recommended)."
                )
        self._crypto = P2PCrypto(p2p_key)
        self._signer = P2PSign(p2p_key)

    async def start(self) -> None:
        """Register the local mDNS service and start listening for peers."""
        self.config.assert_sovereign("beam_p2p_start")
        self._azc = AsyncZeroconf()
        info = ServiceInfo(
            self.service_type,
            f"{self.device_name}.{self.service_type}",
            addresses=[socket.inet_aton(self.local_ip)],
            port=self.beam_port,
            properties={},
        )
        await self._azc.async_register_service(info)
        # BeamNode structurally implements zeroconf's ServiceListener protocol
        # (add_service/update_service/remove_service below) but doesn't inherit it
        # explicitly — ServiceListener is only importable when the optional
        # zeroconf dependency is present, so we avoid a hard subclass.
        self._browser = AsyncServiceBrowser(self._azc.zeroconf, self.service_type, self)  # type: ignore[arg-type]
        # Bind to the configured Beam address (default all-interfaces: Beam
        # exists to serve trusted LAN peers; frames are encrypted + signed).
        self._server = await asyncio.start_server(self._handle_peer, self.local_ip, self.beam_port)

    async def stop(self) -> None:
        """Unregister mDNS services and shut down the peer listener."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._browser:
            await self._browser.async_cancel()
            self._browser = None
        if self._azc:
            await self._azc.async_unregister_all_services()
            await self._azc.async_close()
            self._azc = None

    def rotate_key(self, new_key: str) -> None:
        """Rotate the P2P encryption/signing key without recreating the node.

        Pro-gated (``advanced_p2p``): live key-rotation is a commercial Advanced
        P2P capability. Basic single-key LAN sharing (constructing a ``BeamNode``
        and running ``start()``) stays free in Community.

        After rotation, frames are encrypted/signed with ``new_key``; peers must
        rotate to the same key to keep exchanging frames.

        The signer is rekeyed in place rather than rebuilt, so the seen-nonce set
        survives the rotation and rotation cannot be used to clear it.
        """
        self.config.assert_licensed("advanced_p2p")
        if not new_key:
            raise ValueError("rotate_key requires a non-empty new_key")
        self._crypto = P2PCrypto(new_key)
        self._signer.rekey(new_key)
        self.config.p2p_encryption_key = new_key
        logger.info("beam_key_rotated device=%s", self.device_name)

    async def _handle_peer(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle an incoming framed packet and store its chunks."""
        try:
            packet = await decode_frame(reader, crypto=self._crypto, signer=self._signer)
            if not isinstance(packet, dict):
                return

            # TTL enforcement: a packet expires ttl_hours AFTER its send time.
            ttl_hours = packet.get("ttl")
            sent_at = packet.get("_sent_at")
            if ttl_hours is not None and sent_at:
                try:
                    sent_dt = datetime.fromisoformat(sent_at)
                    expires_dt = sent_dt + timedelta(hours=float(ttl_hours))
                    if datetime.now(timezone.utc) > expires_dt:
                        logger.debug("beam_ttl_expired room=%s", packet.get("room"))
                        return
                except (ValueError, TypeError):
                    logger.warning("beam_ttl_unparseable sent_at=%r ttl=%r", sent_at, ttl_hours)
                    return

            room = packet.get("room")
            chunks = packet.get("chunks", [])
            if not room or not isinstance(chunks, list):
                return
            conn = get_connection(self.config)
            try:
                for chunk in chunks:
                    content = chunk.get("content")
                    if not content:
                        continue
                    chunk_id = store_memory(
                        conn,
                        content,
                        room_name=room,
                        source_type="p2p_share",
                        config=self.config,
                    )
                    conn.execute(
                        "UPDATE provenance SET confidence = 0.5 "
                        "WHERE chunk_id = ? AND source_type = 'p2p_share'",
                        (chunk_id,),
                    )
            finally:
                conn.close()
        except asyncio.IncompleteReadError:
            pass
        except Exception as exc:
            logger.warning("beam_handle_peer_error: %s", exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def share_room(self, room_name: str, ttl_hours: int = 24) -> None:
        """Fetch all active chunks from *room_name* and broadcast them to peers."""
        self.config.assert_sovereign("beam_p2p_share")
        conn = get_connection(self.config)
        try:
            rows = conn.execute(
                "SELECT chunk_id, content, vm_score, content_hash FROM chunk "
                "JOIN room USING(room_id) WHERE room.name = ? AND valid_to IS NULL",
                (room_name,),
            ).fetchall()
        finally:
            conn.close()

        packet: dict = {
            "room": room_name,
            "ttl": ttl_hours,
            "_sent_at": datetime.now(timezone.utc).isoformat(),
            "chunks": [{"content": r[1], "vm": r[2], "hash": r[3]} for r in rows],
        }

        for peer_addr in list(self.peers.values()):
            await self._send(peer_addr, packet)

    async def _send(self, addr: tuple[str, int], packet: dict) -> None:
        """Open a TCP connection to *addr*, send the framed packet, and close."""
        self.config.assert_sovereign("beam_p2p_send")
        host, port = addr
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=5.0
            )
            framed = encode_frame(packet, crypto=self._crypto, signer=self._signer)
            writer.write(framed)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except Exception as exc:
            logger.warning("beam_send_failed to %s:%s: %s", host, port, exc)

    # --- Zeroconf listener callbacks ---

    def add_service(self, zc: Any, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info and info.addresses:
            host = socket.inet_ntoa(info.addresses[0])
            port = info.port
            self.peers[name] = (host, port)
            logger.info("beam peer added: %s at %s:%s", name, host, port)

    def update_service(self, zc: Any, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Any, type_: str, name: str) -> None:
        if name in self.peers:
            del self.peers[name]
            logger.info("beam peer removed: %s", name)
