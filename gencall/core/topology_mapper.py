"""
GenCall SIP Topology Mapper - Network discovery via SIP probing.

Sends SIP OPTIONS / INVITE messages and analyses Via headers, received
parameters, Server/User-Agent headers, and response timing to build
a graph of the SIP network topology between the sender and the target.

Detects:
  - SIP proxies / SBCs along the path
  - NAT (Via sent-by vs received parameter mismatch)
  - SIP ALGs (modified headers)
  - Server software versions
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re
import socket
import time
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("gencall.topology_mapper")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class NodeRole(Enum):
    UNKNOWN = "unknown"
    UAC = "uac"
    UAS = "uas"
    PROXY = "proxy"
    SBC = "sbc"
    NAT = "nat"
    ALG = "alg"
    REGISTRAR = "registrar"
    REDIRECT = "redirect"


class ProbeMethod(Enum):
    OPTIONS = "OPTIONS"
    INVITE = "INVITE"


@dataclass
class TopologyNode:
    """A single node (hop) in the SIP topology."""
    node_id: str = ""
    address: str = ""
    port: int = 5060
    transport: str = "udp"
    hostname: str = ""
    server_software: str = ""
    role: NodeRole = NodeRole.UNKNOWN
    hop_index: int = 0
    response_time_ms: float = 0.0
    via_received: str = ""
    via_rport: int = 0
    nat_detected: bool = False
    alg_detected: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.node_id:
            seed = f"{self.address}:{self.port}:{time.monotonic_ns()}"
            self.node_id = hashlib.sha256(seed.encode()).hexdigest()[:10]

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "address": self.address,
            "port": self.port,
            "transport": self.transport,
            "hostname": self.hostname,
            "server_software": self.server_software,
            "role": self.role.value,
            "hop_index": self.hop_index,
            "response_time_ms": round(self.response_time_ms, 2),
            "via_received": self.via_received,
            "via_rport": self.via_rport,
            "nat_detected": self.nat_detected,
            "alg_detected": self.alg_detected,
            "extra": self.extra,
        }


@dataclass
class TopologyEdge:
    """A link between two topology nodes."""
    source_id: str = ""
    target_id: str = ""
    latency_ms: float = 0.0
    transport: str = "udp"

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "latency_ms": round(self.latency_ms, 2),
            "transport": self.transport,
        }


@dataclass
class TopologyGraph:
    """Graph of discovered SIP network nodes and links."""
    graph_id: str = ""
    nodes: list[TopologyNode] = field(default_factory=list)
    edges: list[TopologyEdge] = field(default_factory=list)
    probe_target: str = ""
    probe_port: int = 5060
    created_at: Optional[datetime.datetime] = None
    nat_detected: bool = False
    alg_detected: bool = False
    total_hops: int = 0
    end_to_end_ms: float = 0.0

    def __post_init__(self) -> None:
        if not self.graph_id:
            self.graph_id = uuid.uuid4().hex[:12]

    def get_node(self, node_id: str) -> Optional[TopologyNode]:
        for n in self.nodes:
            if n.node_id == node_id:
                return n
        return None

    def add_node(self, node: TopologyNode) -> None:
        self.nodes.append(node)

    def add_edge(self, edge: TopologyEdge) -> None:
        self.edges.append(edge)

    def to_dict(self) -> dict:
        return {
            "graph_id": self.graph_id,
            "probe_target": self.probe_target,
            "probe_port": self.probe_port,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "nat_detected": self.nat_detected,
            "alg_detected": self.alg_detected,
            "total_hops": self.total_hops,
            "end_to_end_ms": round(self.end_to_end_ms, 2),
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
        }


@dataclass
class ProbeResult:
    """Raw result from a single SIP probe."""
    method: str = "OPTIONS"
    target: str = ""
    target_port: int = 5060
    local_ip: str = ""
    local_port: int = 0
    response_code: int = 0
    reason_phrase: str = ""
    response_time_ms: float = 0.0
    via_headers: list[str] = field(default_factory=list)
    server_header: str = ""
    user_agent_header: str = ""
    raw_response: str = ""
    error: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "target": self.target,
            "target_port": self.target_port,
            "local_ip": self.local_ip,
            "local_port": self.local_port,
            "response_code": self.response_code,
            "reason_phrase": self.reason_phrase,
            "response_time_ms": round(self.response_time_ms, 2),
            "via_headers": self.via_headers,
            "server_header": self.server_header,
            "user_agent_header": self.user_agent_header,
            "error": self.error,
            "timestamp": round(self.timestamp, 3),
        }


# ---------------------------------------------------------------------------
# SIP message builder
# ---------------------------------------------------------------------------

_BRANCH_MAGIC = "z9hG4bK"
_VIA_RE = re.compile(
    r"Via\s*:\s*SIP/2\.0/(\w+)\s+([\w\.\-\[\]:]+?)(?::(\d+))?"
    r"((?:\s*;\s*\w+(?:=[\w\.\-\[\]:]+)?)*)",
    re.IGNORECASE,
)
_PARAM_RE = re.compile(r";(\w+)=?([\w\.\-\[\]:]*)")
_HEADER_RE = re.compile(r"^([\w\-]+)\s*:\s*(.+)$", re.MULTILINE)


def _generate_branch() -> str:
    return f"{_BRANCH_MAGIC}{uuid.uuid4().hex[:16]}"


def _generate_tag() -> str:
    return uuid.uuid4().hex[:8]


def _generate_call_id() -> str:
    return f"{uuid.uuid4().hex[:16]}@gencall"


def _build_options(
    target: str,
    target_port: int,
    local_ip: str,
    local_port: int,
    transport: str = "UDP",
) -> str:
    branch = _generate_branch()
    tag = _generate_tag()
    call_id = _generate_call_id()
    return (
        f"OPTIONS sip:{target}:{target_port} SIP/2.0\r\n"
        f"Via: SIP/2.0/{transport.upper()} {local_ip}:{local_port};branch={branch};rport\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:probe@{local_ip}>;tag={tag}\r\n"
        f"To: <sip:{target}:{target_port}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Contact: <sip:probe@{local_ip}:{local_port}>\r\n"
        f"Accept: application/sdp\r\n"
        f"Content-Length: 0\r\n"
        f"User-Agent: GenCall/2.0 TopologyMapper\r\n"
        f"\r\n"
    )


def _build_invite(
    target: str,
    target_port: int,
    local_ip: str,
    local_port: int,
    transport: str = "UDP",
) -> str:
    branch = _generate_branch()
    tag = _generate_tag()
    call_id = _generate_call_id()
    sdp_body = (
        "v=0\r\n"
        f"o=gencall 0 0 IN IP4 {local_ip}\r\n"
        "s=topology-probe\r\n"
        f"c=IN IP4 {local_ip}\r\n"
        "t=0 0\r\n"
        "m=audio 0 RTP/AVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    )
    return (
        f"INVITE sip:probe@{target}:{target_port} SIP/2.0\r\n"
        f"Via: SIP/2.0/{transport.upper()} {local_ip}:{local_port};branch={branch};rport\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:mapper@{local_ip}>;tag={tag}\r\n"
        f"To: <sip:probe@{target}>\r\n"
        f"Call-ID: {call_id}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:mapper@{local_ip}:{local_port}>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp_body)}\r\n"
        f"User-Agent: GenCall/2.0 TopologyMapper\r\n"
        f"\r\n"
        f"{sdp_body}"
    )


# ---------------------------------------------------------------------------
# Response parser helpers
# ---------------------------------------------------------------------------

def _parse_via_headers(raw: str) -> list[dict[str, Any]]:
    """Extract all Via headers with parameters."""
    results: list[dict[str, Any]] = []

    for match in _VIA_RE.finditer(raw):
        transport = match.group(1).upper()
        host = match.group(2)
        port_str = match.group(3)
        params_str = match.group(4) or ""

        port = int(port_str) if port_str else 5060
        params: dict[str, str] = {}
        for pm in _PARAM_RE.finditer(params_str):
            params[pm.group(1).lower()] = pm.group(2)

        results.append({
            "transport": transport,
            "host": host,
            "port": port,
            "branch": params.get("branch", ""),
            "received": params.get("received", ""),
            "rport": int(params["rport"]) if params.get("rport") else 0,
        })

    return results


def _extract_header(raw: str, name: str) -> str:
    for m in _HEADER_RE.finditer(raw):
        if m.group(1).lower() == name.lower():
            return m.group(2).strip()
    return ""


def _parse_response_line(raw: str) -> tuple[int, str]:
    first_line = raw.split("\r\n", 1)[0].split("\n", 1)[0]
    parts = first_line.split(None, 2)
    if len(parts) >= 2 and parts[0].startswith("SIP/"):
        code = int(parts[1]) if parts[1].isdigit() else 0
        reason = parts[2] if len(parts) > 2 else ""
        return code, reason
    return 0, ""


# ---------------------------------------------------------------------------
# Topology Mapper
# ---------------------------------------------------------------------------

class TopologyMapper:
    """
    Discovers SIP network topology by probing a target.

    Usage::

        mapper = TopologyMapper("pbx.example.com", 5060)
        graph = mapper.discover()
        print(mapper.render_diagram(graph))
    """

    def __init__(
        self,
        target: str,
        port: int = 5060,
        transport: str = "udp",
        local_ip: str = "0.0.0.0",
        local_port: int = 0,
        timeout: float = 5.0,
        max_retries: int = 2,
    ) -> None:
        self.target = target
        self.port = port
        self.transport = transport.lower()
        self.local_ip = local_ip
        self.local_port = local_port
        self.timeout = timeout
        self.max_retries = max_retries

    # -- public API --------------------------------------------------------

    def discover(
        self,
        methods: Optional[list[ProbeMethod]] = None,
    ) -> TopologyGraph:
        """
        Run topology discovery probes and return the graph.

        Sends OPTIONS (and optionally INVITE) to the target, then
        analyses the response Via headers to map intermediate hops.
        """
        methods = methods or [ProbeMethod.OPTIONS]

        graph = TopologyGraph(
            probe_target=self.target,
            probe_port=self.port,
            created_at=datetime.datetime.utcnow(),
        )

        probes: list[ProbeResult] = []
        for method in methods:
            result = self._send_probe(method)
            probes.append(result)
            if result.error:
                logger.warning("Probe %s failed: %s", method.value, result.error)

        # Build the graph from the best probe (prefer one with a response)
        best = next((p for p in probes if p.response_code > 0), None)
        if best is None:
            logger.warning("No successful probes; graph will be empty")
            if probes:
                graph.end_to_end_ms = 0.0
            return graph

        self._build_graph(graph, best)
        self._detect_anomalies(graph, best)

        logger.info(
            "Topology mapped: %d nodes, %d edges, NAT=%s, ALG=%s, e2e=%.1fms",
            len(graph.nodes), len(graph.edges),
            graph.nat_detected, graph.alg_detected, graph.end_to_end_ms,
        )
        return graph

    def probe_options(self) -> ProbeResult:
        """Send a single OPTIONS probe."""
        return self._send_probe(ProbeMethod.OPTIONS)

    def probe_invite(self) -> ProbeResult:
        """Send a single INVITE probe."""
        return self._send_probe(ProbeMethod.INVITE)

    # -- diagram -----------------------------------------------------------

    @staticmethod
    def render_diagram(graph: TopologyGraph) -> str:
        """Generate a text-based network topology diagram."""
        if not graph.nodes:
            return "(no nodes discovered)"

        lines: list[str] = []
        lines.append("=" * 70)
        lines.append("  SIP Network Topology Diagram")
        lines.append(f"  Target: {graph.probe_target}:{graph.probe_port}")
        lines.append(f"  Discovered: {graph.created_at.isoformat() if graph.created_at else 'N/A'}")
        lines.append(f"  End-to-end: {graph.end_to_end_ms:.1f} ms")
        if graph.nat_detected:
            lines.append("  *** NAT DETECTED ***")
        if graph.alg_detected:
            lines.append("  *** SIP ALG DETECTED ***")
        lines.append("=" * 70)
        lines.append("")

        sorted_nodes = sorted(graph.nodes, key=lambda n: n.hop_index)

        for i, node in enumerate(sorted_nodes):
            # Node box
            label = f"{node.address}:{node.port}"
            role_str = f" [{node.role.value.upper()}]"
            sw_str = f" ({node.server_software})" if node.server_software else ""
            nat_str = " *NAT*" if node.nat_detected else ""
            alg_str = " *ALG*" if node.alg_detected else ""

            box_content = f"{label}{role_str}{sw_str}{nat_str}{alg_str}"
            box_width = max(len(box_content) + 4, 30)

            lines.append("    " + "+" + "-" * (box_width - 2) + "+")
            lines.append("    " + "| " + box_content.ljust(box_width - 4) + " |")

            if node.hostname and node.hostname != node.address:
                host_line = f"  {node.hostname}"
                lines.append("    " + "| " + host_line.ljust(box_width - 4) + " |")

            timing_line = f"  RTT: {node.response_time_ms:.1f} ms"
            lines.append("    " + "| " + timing_line.ljust(box_width - 4) + " |")
            lines.append("    " + "+" + "-" * (box_width - 2) + "+")

            # Draw connector to next node
            if i < len(sorted_nodes) - 1:
                lines.append("    " + " " * (box_width // 2) + "|")
                # Find the edge latency
                edge_lat = 0.0
                next_node = sorted_nodes[i + 1]
                for edge in graph.edges:
                    if edge.source_id == node.node_id and edge.target_id == next_node.node_id:
                        edge_lat = edge.latency_ms
                        break
                lat_label = f" {edge_lat:.1f}ms " if edge_lat > 0 else ""
                lines.append("    " + " " * (box_width // 2 - len(lat_label) // 2) + lat_label)
                lines.append("    " + " " * (box_width // 2) + "|")
                lines.append("    " + " " * (box_width // 2) + "v")

        lines.append("")
        lines.append(f"  Total hops: {graph.total_hops}")
        lines.append("=" * 70)
        return "\n".join(lines)

    # -- probing -----------------------------------------------------------

    def _send_probe(self, method: ProbeMethod) -> ProbeResult:
        """Send a SIP probe message and capture the response."""
        result = ProbeResult(
            method=method.value,
            target=self.target,
            target_port=self.port,
        )

        try:
            resolved = socket.getaddrinfo(self.target, self.port, socket.AF_UNSPEC, socket.SOCK_DGRAM)
            if not resolved:
                result.error = f"Cannot resolve {self.target}"
                return result

            family = resolved[0][0]
            target_addr = resolved[0][4]

            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)

            if self.local_port:
                sock.bind((self.local_ip, self.local_port))
            else:
                sock.bind((self.local_ip, 0))

            bound = sock.getsockname()
            local_ip = bound[0]
            local_port = bound[1]

            # Use the bound local IP; if 0.0.0.0 try to discover real IP
            if local_ip in ("0.0.0.0", "::"):
                local_ip = self._discover_local_ip(self.target)

            result.local_ip = local_ip
            result.local_port = local_port

            if method == ProbeMethod.OPTIONS:
                msg = _build_options(self.target, self.port, local_ip, local_port, self.transport)
            else:
                msg = _build_invite(self.target, self.port, local_ip, local_port, self.transport)

            # Send with retries
            for attempt in range(self.max_retries + 1):
                send_time = time.monotonic()
                sock.sendto(msg.encode("utf-8"), target_addr)

                try:
                    data, addr = sock.recvfrom(65535)
                    recv_time = time.monotonic()
                    result.response_time_ms = (recv_time - send_time) * 1000.0
                    result.raw_response = data.decode("utf-8", errors="replace")
                    break
                except socket.timeout:
                    if attempt == self.max_retries:
                        result.error = f"Timeout after {self.max_retries + 1} attempts"
                    continue

            sock.close()

            if result.raw_response:
                result.response_code, result.reason_phrase = _parse_response_line(result.raw_response)
                result.via_headers = [
                    line.strip()
                    for line in result.raw_response.split("\n")
                    if line.strip().lower().startswith("via:")
                ]
                result.server_header = _extract_header(result.raw_response, "Server")
                result.user_agent_header = _extract_header(result.raw_response, "User-Agent")

        except Exception as exc:
            result.error = str(exc)
            logger.debug("Probe error: %s", exc, exc_info=True)

        return result

    # -- graph construction ------------------------------------------------

    def _build_graph(self, graph: TopologyGraph, probe: ProbeResult) -> None:
        """Build the topology graph from Via headers in the probe response."""
        vias = _parse_via_headers(probe.raw_response)

        # Our own node (UAC)
        uac_node = TopologyNode(
            address=probe.local_ip,
            port=probe.local_port,
            transport=self.transport,
            role=NodeRole.UAC,
            hop_index=0,
            response_time_ms=probe.response_time_ms,
        )
        graph.add_node(uac_node)

        # Via headers are in reverse order (top Via = our Via, then each proxy
        # adds its own at the top).  The response echoes them in order:
        # topmost Via = first hop (us), bottom Via = last proxy before target.
        # Actually the target puts them in the same order - first Via is ours.

        prev_node = uac_node
        for idx, via in enumerate(vias):
            via_host = via.get("received") or via["host"]
            via_port = via.get("rport") or via["port"]

            # Skip our own Via
            if idx == 0 and via_host == probe.local_ip:
                continue

            hop_node = TopologyNode(
                address=via_host,
                port=via_port,
                transport=via.get("transport", self.transport),
                role=NodeRole.PROXY,
                hop_index=idx,
            )

            # Try reverse DNS
            try:
                hostname = socket.getfqdn(via_host)
                if hostname != via_host:
                    hop_node.hostname = hostname
            except Exception:
                pass

            # Detect NAT for this hop
            original_host = via["host"]
            received = via.get("received", "")
            if received and received != original_host:
                hop_node.nat_detected = True
                hop_node.via_received = received
                graph.nat_detected = True

            if via.get("rport") and via["rport"] != via["port"]:
                hop_node.via_rport = via["rport"]

            graph.add_node(hop_node)
            edge = TopologyEdge(
                source_id=prev_node.node_id,
                target_id=hop_node.node_id,
                transport=self.transport,
            )
            graph.add_edge(edge)
            prev_node = hop_node

        # Final target node (UAS)
        server_sw = probe.server_header or probe.user_agent_header
        uas_node = TopologyNode(
            address=self.target,
            port=self.port,
            transport=self.transport,
            hostname=self.target,
            server_software=server_sw,
            role=NodeRole.UAS,
            hop_index=len(vias) + 1,
            response_time_ms=probe.response_time_ms,
        )
        graph.add_node(uas_node)
        edge = TopologyEdge(
            source_id=prev_node.node_id,
            target_id=uas_node.node_id,
            latency_ms=probe.response_time_ms,
            transport=self.transport,
        )
        graph.add_edge(edge)

        graph.total_hops = len(graph.nodes) - 1  # exclude UAC
        graph.end_to_end_ms = probe.response_time_ms

    def _detect_anomalies(self, graph: TopologyGraph, probe: ProbeResult) -> None:
        """Detect NAT and ALG anomalies in the probe response."""
        vias = _parse_via_headers(probe.raw_response)

        if not vias:
            return

        # Our original Via
        our_via = vias[0] if vias else None
        if our_via:
            sent_host = our_via["host"]
            received = our_via.get("received", "")
            if received and received != sent_host:
                graph.nat_detected = True
                logger.info(
                    "NAT detected: sent-by=%s, received=%s", sent_host, received,
                )

        # ALG detection: look for modified Via branch or mangled headers
        for via in vias:
            branch = via.get("branch", "")
            if branch and not branch.startswith(_BRANCH_MAGIC):
                graph.alg_detected = True
                logger.info("Possible SIP ALG: non-standard branch=%s", branch)
                # Mark nodes
                for node in graph.nodes:
                    if node.address == via["host"]:
                        node.alg_detected = True
                        node.role = NodeRole.ALG

    # -- utilities ---------------------------------------------------------

    @staticmethod
    def _discover_local_ip(target: str) -> str:
        """Discover the local IP that would be used to reach *target*."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((target, 1))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
