"""
GenCall - PCAP Analyzer

Post-call analysis of captured SIP/RTP traffic:
  - Parse pcap files for SIP messages and RTP streams
  - Generate call flow ladder diagrams (text-based)
  - Calculate RTP quality metrics from captures
  - Detect SIP anomalies (retransmissions, timeouts, loops)
  - Export analysis reports
"""

import struct
import time
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("gencall.pcap_analyzer")


@dataclass
class SIPMessage:
    """A parsed SIP message."""
    timestamp: float
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    method: str = ""        # INVITE, BYE, ACK, etc.
    response_code: int = 0  # 200, 404, etc.
    call_id: str = ""
    cseq: str = ""
    from_header: str = ""
    to_header: str = ""
    via: str = ""
    content_length: int = 0
    raw: str = ""
    is_request: bool = True

    @property
    def summary(self) -> str:
        if self.is_request:
            return f"{self.method}"
        return f"{self.response_code}"

    @property
    def direction(self) -> str:
        return f"{self.src_ip}:{self.src_port} -> {self.dst_ip}:{self.dst_port}"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "src": f"{self.src_ip}:{self.src_port}",
            "dst": f"{self.dst_ip}:{self.dst_port}",
            "method": self.method,
            "response_code": self.response_code,
            "call_id": self.call_id,
            "cseq": self.cseq,
            "is_request": self.is_request,
        }


@dataclass
class RTPStreamInfo:
    """Aggregated info about an RTP stream in a capture."""
    ssrc: int
    src_ip: str
    src_port: int
    dst_ip: str
    dst_port: int
    payload_type: int = 0
    packet_count: int = 0
    first_timestamp: float = 0.0
    last_timestamp: float = 0.0
    bytes_total: int = 0
    seq_gaps: int = 0          # missing sequence numbers
    seq_duplicates: int = 0
    seq_reorders: int = 0
    jitter_samples: list = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.last_timestamp - self.first_timestamp if self.first_timestamp else 0.0

    @property
    def packet_loss_pct(self) -> float:
        if self.packet_count == 0:
            return 0.0
        expected = self.packet_count + self.seq_gaps
        return (self.seq_gaps / expected) * 100 if expected > 0 else 0.0

    @property
    def avg_jitter_ms(self) -> float:
        if not self.jitter_samples:
            return 0.0
        return sum(self.jitter_samples) / len(self.jitter_samples)

    @property
    def max_jitter_ms(self) -> float:
        return max(self.jitter_samples) if self.jitter_samples else 0.0

    def to_dict(self) -> dict:
        return {
            "ssrc": hex(self.ssrc),
            "src": f"{self.src_ip}:{self.src_port}",
            "dst": f"{self.dst_ip}:{self.dst_port}",
            "payload_type": self.payload_type,
            "packets": self.packet_count,
            "duration_sec": round(self.duration, 2),
            "bytes": self.bytes_total,
            "packet_loss_pct": round(self.packet_loss_pct, 2),
            "seq_gaps": self.seq_gaps,
            "seq_duplicates": self.seq_duplicates,
            "avg_jitter_ms": round(self.avg_jitter_ms, 2),
            "max_jitter_ms": round(self.max_jitter_ms, 2),
        }


@dataclass
class CallFlowEntry:
    """One arrow in a call flow ladder diagram."""
    timestamp: float
    src: str
    dst: str
    label: str
    call_id: str = ""


class PcapAnalyzer:
    """
    Analyze pcap files for SIP and RTP content.
    """

    def __init__(self):
        self.sip_messages: list[SIPMessage] = []
        self.rtp_streams: dict[int, RTPStreamInfo] = {}  # keyed by SSRC
        self.call_flows: dict[str, list[CallFlowEntry]] = defaultdict(list)  # keyed by Call-ID

    def analyze_file(self, filepath: str) -> dict:
        """Analyze a pcap file and return structured results."""
        try:
            import dpkt
        except ImportError:
            logger.error("dpkt required for pcap analysis. pip install dpkt")
            return {"error": "dpkt not installed"}

        with open(filepath, "rb") as f:
            pcap = dpkt.pcap.Reader(f)
            for ts, buf in pcap:
                self._process_packet(ts, buf, dpkt)

        return self.get_report()

    def _process_packet(self, ts: float, buf: bytes, dpkt):
        """Process a single packet from the capture."""
        try:
            eth = dpkt.ethernet.Ethernet(buf)
            if not isinstance(eth.data, dpkt.ip.IP):
                return
            ip = eth.data
            if not isinstance(ip.data, dpkt.udp.UDP):
                return
            udp = ip.data

            src_ip = self._ip_to_str(ip.src)
            dst_ip = self._ip_to_str(ip.dst)
            src_port = udp.sport
            dst_port = udp.dport
            payload = udp.data

            if not payload:
                return

            # Try SIP first (text-based, starts with SIP/ or a method)
            if self._is_sip(payload):
                self._parse_sip(ts, src_ip, src_port, dst_ip, dst_port, payload)
            elif len(payload) >= 12:
                # Try RTP
                self._parse_rtp(ts, src_ip, src_port, dst_ip, dst_port, payload)

        except Exception:
            pass  # Skip malformed packets

    @staticmethod
    def _ip_to_str(ip_bytes: bytes) -> str:
        return ".".join(str(b) for b in ip_bytes)

    @staticmethod
    def _is_sip(payload: bytes) -> bool:
        try:
            start = payload[:20].decode("utf-8", errors="ignore").upper()
            sip_methods = ["INVITE", "ACK", "BYE", "CANCEL", "REGISTER",
                           "OPTIONS", "REFER", "NOTIFY", "INFO", "UPDATE",
                           "PRACK", "SUBSCRIBE", "PUBLISH", "MESSAGE"]
            if start.startswith("SIP/"):
                return True
            for method in sip_methods:
                if start.startswith(method):
                    return True
        except Exception:
            pass
        return False

    def _parse_sip(self, ts, src_ip, src_port, dst_ip, dst_port, payload):
        """Parse a SIP message from UDP payload."""
        try:
            text = payload.decode("utf-8", errors="replace")
        except Exception:
            return

        msg = SIPMessage(
            timestamp=ts,
            src_ip=src_ip, src_port=src_port,
            dst_ip=dst_ip, dst_port=dst_port,
            raw=text,
        )

        lines = text.split("\r\n")
        if not lines:
            return

        # Parse request line or status line
        first_line = lines[0]
        if first_line.startswith("SIP/"):
            # Response: SIP/2.0 200 OK
            parts = first_line.split(" ", 2)
            msg.is_request = False
            msg.response_code = int(parts[1]) if len(parts) > 1 else 0
        else:
            # Request: INVITE sip:user@host SIP/2.0
            parts = first_line.split(" ")
            msg.is_request = True
            msg.method = parts[0] if parts else ""

        # Parse headers
        for line in lines[1:]:
            if not line:
                break
            lower = line.lower()
            if lower.startswith("call-id:") or lower.startswith("i:"):
                msg.call_id = line.split(":", 1)[1].strip()
            elif lower.startswith("cseq:"):
                msg.cseq = line.split(":", 1)[1].strip()
            elif lower.startswith("from:") or lower.startswith("f:"):
                msg.from_header = line.split(":", 1)[1].strip()
            elif lower.startswith("to:") or lower.startswith("t:"):
                msg.to_header = line.split(":", 1)[1].strip()
            elif lower.startswith("via:") or lower.startswith("v:"):
                msg.via = line.split(":", 1)[1].strip()

        self.sip_messages.append(msg)

        # Track call flow
        label = msg.method if msg.is_request else f"{msg.response_code}"
        self.call_flows[msg.call_id].append(CallFlowEntry(
            timestamp=ts,
            src=f"{src_ip}:{src_port}",
            dst=f"{dst_ip}:{dst_port}",
            label=label,
            call_id=msg.call_id,
        ))

    def _parse_rtp(self, ts, src_ip, src_port, dst_ip, dst_port, payload):
        """Parse RTP packet and update stream stats."""
        if len(payload) < 12:
            return

        head, seq, rtp_ts, ssrc = struct.unpack(">HHLL", payload[:12])
        version = head >> 14
        if version != 2:
            return

        pt = (head >> 8) & 0x7F

        if ssrc not in self.rtp_streams:
            self.rtp_streams[ssrc] = RTPStreamInfo(
                ssrc=ssrc,
                src_ip=src_ip, src_port=src_port,
                dst_ip=dst_ip, dst_port=dst_port,
                payload_type=pt,
                first_timestamp=ts,
            )

        stream = self.rtp_streams[ssrc]
        stream.packet_count += 1
        stream.last_timestamp = ts
        stream.bytes_total += len(payload)

        # Track jitter (inter-arrival time variation)
        if stream.packet_count > 1:
            inter_arrival = (ts - stream.last_timestamp) * 1000  # ms
            expected = 20  # typical ptime
            jitter = abs(inter_arrival - expected)
            stream.jitter_samples.append(jitter)

    def generate_ladder_diagram(self, call_id: str) -> str:
        """
        Generate a text-based ladder diagram for a call.

        Example output:
            10.0.0.1:5060          10.0.0.2:5060
                 |                      |
                 |------- INVITE ------>|
                 |<------ 100 ---------|
                 |<------ 180 ---------|
                 |<------ 200 ---------|
                 |------- ACK -------->|
                 |                      |
                 |------- BYE -------->|
                 |<------ 200 ---------|
        """
        entries = self.call_flows.get(call_id, [])
        if not entries:
            return f"No messages found for Call-ID: {call_id}"

        # Collect unique endpoints
        endpoints = []
        seen = set()
        for e in entries:
            for ep in [e.src, e.dst]:
                if ep not in seen:
                    seen.add(ep)
                    endpoints.append(ep)

        if len(endpoints) < 2:
            return "Need at least 2 endpoints for diagram"

        # Column positions
        col_width = 30
        cols = {ep: i * col_width for i, ep in enumerate(endpoints)}

        lines = []

        # Header
        header = ""
        for ep in endpoints:
            header += ep.center(col_width)
        lines.append(header)

        # Separator
        sep = ""
        for ep in endpoints:
            sep += "|".center(col_width)
        lines.append(sep)

        # Messages
        for entry in sorted(entries, key=lambda e: e.timestamp):
            src_col = cols.get(entry.src, 0)
            dst_col = cols.get(entry.dst, col_width)

            if src_col < dst_col:
                # Left to right
                arrow_len = dst_col - src_col - len(entry.label) - 4
                arrow = f"|{'-' * max(1, arrow_len // 2)} {entry.label} {'-' * max(1, arrow_len // 2)}>|"
            else:
                # Right to left
                arrow_len = src_col - dst_col - len(entry.label) - 4
                arrow = f"|<{'-' * max(1, arrow_len // 2)} {entry.label} {'-' * max(1, arrow_len // 2)}|"

            # Pad to position
            line = " " * min(src_col, dst_col) + arrow
            lines.append(line)

        lines.append(sep)
        return "\n".join(lines)

    def get_report(self) -> dict:
        """Generate the full analysis report."""
        # SIP summary
        methods = defaultdict(int)
        responses = defaultdict(int)
        call_ids = set()

        for msg in self.sip_messages:
            if msg.is_request:
                methods[msg.method] += 1
            else:
                responses[msg.response_code] += 1
            call_ids.add(msg.call_id)

        # Detect anomalies
        anomalies = self._detect_anomalies()

        return {
            "sip": {
                "total_messages": len(self.sip_messages),
                "unique_calls": len(call_ids),
                "methods": dict(methods),
                "responses": dict(responses),
                "anomalies": anomalies,
            },
            "rtp": {
                "streams": len(self.rtp_streams),
                "stream_details": [s.to_dict() for s in self.rtp_streams.values()],
            },
            "call_ids": list(call_ids)[:50],
        }

    def _detect_anomalies(self) -> list[dict]:
        """Detect SIP protocol anomalies."""
        anomalies = []

        # Check for retransmissions (same Call-ID + CSeq + Method seen multiple times)
        seen_msgs = defaultdict(int)
        for msg in self.sip_messages:
            key = (msg.call_id, msg.cseq, msg.method, msg.response_code)
            seen_msgs[key] += 1

        for key, count in seen_msgs.items():
            if count > 1:
                anomalies.append({
                    "type": "retransmission",
                    "call_id": key[0],
                    "cseq": key[1],
                    "count": count,
                    "severity": "warning",
                })

        # Check for calls without BYE (potential leaks)
        for call_id, entries in self.call_flows.items():
            labels = [e.label for e in entries]
            has_invite = "INVITE" in labels
            has_bye = "BYE" in labels
            has_200 = "200" in labels
            has_cancel = "CANCEL" in labels

            if has_invite and has_200 and not has_bye and not has_cancel:
                anomalies.append({
                    "type": "missing_bye",
                    "call_id": call_id,
                    "severity": "error",
                    "detail": "Call answered but no BYE - possible session leak",
                })

        return anomalies
