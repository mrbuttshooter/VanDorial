"""
VLAN fleet discovery via UDP beacons (opt-in).

Same-VLAN boxes are layer-2 adjacent, so a worker can announce itself with a
small UDP broadcast and the controller can auto-register it — no hand-copying
addresses. Trust on a private VLAN is a shared ``fleet_token`` carried in every
beacon AND used as the api_key the controller presents to the discovered worker;
a beacon whose token does not match the controller's is ignored.

This is OPT-IN (``[fleet] announce`` on workers, ``[fleet] discovery`` on the
controller) and complements — does not replace — manual node registration.

Layout:
  * build_beacon / encode_beacon / parse_beacon — pure, fully unit-tested.
  * BeaconBroadcaster — worker side: periodic UDP broadcast (a daemon thread).
  * BeaconListener    — controller side: receive + dispatch beacons to a callback.
  * upsert_discovered_node — register/refresh a controller Node row from a beacon.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from typing import Callable, Optional

logger = logging.getLogger("gencall.discovery")

# Bumped if the wire format changes; a beacon without the exact magic is ignored.
BEACON_MAGIC = "gencall-fleet/1"
MAX_BEACON_BYTES = 2048


# ─── pure wire format ─────────────────────────────────────────────────────────

def build_beacon(token: str, address: str, hostname: str = "", version: str = "") -> dict:
    """Construct the beacon payload a worker broadcasts."""
    return {
        "magic": BEACON_MAGIC,
        "token": token or "",
        "address": address,
        "hostname": hostname or "",
        "version": version or "",
    }


def encode_beacon(beacon: dict) -> bytes:
    return json.dumps(beacon, separators=(",", ":")).encode("utf-8")


def parse_beacon(data: bytes, expected_token: str) -> Optional[dict]:
    """Validate + parse a received datagram. Returns the node info dict
    (``address``/``hostname``/``version``) or None if it is not a valid beacon
    for our fleet (bad JSON, wrong magic, token mismatch, or no address).

    An empty ``expected_token`` rejects ALL beacons (fail closed): set ``[fleet]
    token`` on every box to use discovery, so foreign/forged beacons can't
    auto-register a node on an open VLAN.
    """
    if not data or len(data) > MAX_BEACON_BYTES:
        return None
    try:
        b = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(b, dict) or b.get("magic") != BEACON_MAGIC:
        return None
    # Fail CLOSED: with no fleet token configured, reject EVERY beacon rather than
    # "accept any" — discovery must not auto-register foreign/forged nodes.
    if not expected_token or b.get("token") != expected_token:
        return None
    address = (b.get("address") or "").strip()
    if not address:
        return None
    return {
        "address": address,
        "hostname": (b.get("hostname") or "").strip(),
        "version": (b.get("version") or "").strip(),
    }


# ─── worker side: broadcaster ─────────────────────────────────────────────────

class BeaconBroadcaster:
    """Periodically UDP-broadcast this worker's beacon on the VLAN."""

    def __init__(self, token: str, address: str, *, port: int = 45790,
                 interval: int = 10, hostname: str = "", version: str = ""):
        self.payload = encode_beacon(build_beacon(token, address, hostname, version))
        self.port = port
        self.interval = max(2, int(interval))
        self.address = address
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fleet-beacon",
                                        daemon=True)
        self._thread.start()
        logger.info("Fleet beacon broadcasting %s on udp/%d every %ds",
                    self.address, self.port, self.interval)

    def _run(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            while not self._stop.is_set():
                try:
                    sock.sendto(self.payload, ("255.255.255.255", self.port))
                except OSError as e:
                    logger.debug("beacon send failed: %s", e)
                self._stop.wait(self.interval)
        finally:
            sock.close()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


# ─── controller side: listener ────────────────────────────────────────────────

class BeaconListener:
    """Listen for worker beacons and hand each parsed one to ``on_beacon``."""

    def __init__(self, on_beacon: Callable[[dict], None], *, port: int = 45790,
                 token: str = ""):
        self.on_beacon = on_beacon
        self.port = port
        self.token = token
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self.port))
        self._sock.settimeout(1.0)
        self._thread = threading.Thread(target=self._run, name="fleet-discovery",
                                        daemon=True)
        self._thread.start()
        logger.info("Fleet discovery listening on udp/%d", self.port)

    def _run(self):
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(MAX_BEACON_BYTES)
            except socket.timeout:
                continue
            except OSError:
                break
            info = parse_beacon(data, self.token)
            if info is None:
                continue
            try:
                self.on_beacon(info)
            except Exception as e:  # pragma: no cover - callback is caller's
                logger.warning("discovery callback failed: %s", e)

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)


# ─── controller side: register a discovered node ──────────────────────────────

def upsert_discovered_node(db, info: dict, token: str) -> str:
    """Register or refresh a controller Node (fleet_nodes) from a beacon.

    Matches on ``address`` so a re-announcing worker is updated, not duplicated.
    A new node is created enabled, with ``api_key = token`` (the shared fleet
    secret the controller uses to command it). Returns "created" | "updated".
    """
    from gencall.controller.models import Node

    address = info["address"].rstrip("/")
    name = info.get("hostname") or address
    session = db.get_session()
    try:
        node = session.query(Node).filter_by(address=address).first()
        if node is None:
            node = Node(name=name, address=address, api_key=token, enabled=True)
            session.add(node)
            session.commit()
            logger.info("Discovered new fleet node %s (%s)", name, address)
            return "created"
        # Refresh the key (token may have rotated); keep operator's name/enabled.
        if token and node.api_key != token:
            node.api_key = token
            session.commit()
        return "updated"
    finally:
        session.close()
