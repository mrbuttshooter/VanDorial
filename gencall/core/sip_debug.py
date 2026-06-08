"""
GenCall SIP Message Debugger.
Captures, parses, stores, filters, and visualizes SIP signaling
with text-based call-flow ladder diagrams and hex dumps.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re
import threading
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("gencall.sip_debug")

# ─── SIP Constants ────────────────────────────────────────────────────────────

_SIP_METHODS = frozenset({
    "INVITE", "ACK", "BYE", "CANCEL", "REGISTER", "OPTIONS",
    "PRACK", "SUBSCRIBE", "NOTIFY", "PUBLISH", "INFO",
    "REFER", "MESSAGE", "UPDATE",
})

_HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9\-]*)\s*:\s*(.*)$")

# Compact header name mapping (RFC 3261 Section 7.3.3)
_COMPACT_HEADERS = {
    "i": "Call-ID",
    "m": "Contact",
    "e": "Content-Encoding",
    "l": "Content-Length",
    "c": "Content-Type",
    "f": "From",
    "s": "Subject",
    "k": "Supported",
    "t": "To",
    "v": "Via",
}


# ─── SIP Direction / Type ────────────────────────────────────────────────────

class SIPDirection(Enum):
    SENT = "sent"
    RECEIVED = "received"


class SIPMessageType(Enum):
    REQUEST = "request"
    RESPONSE = "response"


# ─── Parsed SIP Header ───────────────────────────────────────────────────────

@dataclass
class SIPHeader:
    """A single parsed SIP header with potential multi-values."""
    name: str
    values: list[str] = field(default_factory=list)

    @property
    def value(self) -> str:
        return self.values[0] if self.values else ""

    def to_dict(self) -> dict:
        return {"name": self.name, "values": self.values}


# ─── Parsed SDP ──────────────────────────────────────────────────────────────

@dataclass
class SDPInfo:
    """Parsed Session Description Protocol body."""
    version: str = "0"
    origin: str = ""
    session_name: str = ""
    connection: str = ""
    media_lines: list[str] = field(default_factory=list)
    attributes: dict[str, list[str]] = field(default_factory=dict)
    raw: str = ""

    @property
    def media_ip(self) -> str:
        if self.connection:
            parts = self.connection.split()
            if len(parts) >= 3:
                return parts[2]
        return ""

    @property
    def media_port(self) -> int:
        for m in self.media_lines:
            parts = m.split()
            if len(parts) >= 2 and parts[0] == "audio":
                try:
                    return int(parts[1])
                except ValueError:
                    pass
        return 0

    @property
    def codecs(self) -> list[str]:
        """Extract codec names from rtpmap attributes."""
        result: list[str] = []
        for val in self.attributes.get("rtpmap", []):
            # "8 PCMA/8000"
            parts = val.split(None, 1)
            if len(parts) >= 2:
                result.append(parts[1].split("/")[0])
        return result

    @property
    def codec_ids(self) -> list[int]:
        """Extract numeric codec payload types from the m= line."""
        for m in self.media_lines:
            match = re.match(r"audio\s+\d+\s+RTP/AVP\s+([\d\s]+)", m)
            if match:
                return [int(c) for c in match.group(1).split()]
        return []

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "origin": self.origin,
            "session_name": self.session_name,
            "connection": self.connection,
            "media_ip": self.media_ip,
            "media_port": self.media_port,
            "codecs": self.codecs,
            "codec_ids": self.codec_ids,
            "media_lines": self.media_lines,
            "attributes": self.attributes,
        }


# ─── Parsed SIP Message ──────────────────────────────────────────────────────

@dataclass
class SIPMessage:
    """A fully parsed SIP message."""

    # Identity
    message_id: str = ""
    timestamp: datetime.datetime = field(default_factory=datetime.datetime.utcnow)
    direction: SIPDirection = SIPDirection.RECEIVED

    # Network
    source_ip: str = ""
    source_port: int = 0
    dest_ip: str = ""
    dest_port: int = 0
    transport: str = "udp"

    # Parsed start line
    message_type: SIPMessageType = SIPMessageType.REQUEST
    method: str = ""
    request_uri: str = ""
    status_code: int = 0
    reason_phrase: str = ""
    sip_version: str = "SIP/2.0"

    # Headers (ordered)
    headers: dict[str, SIPHeader] = field(default_factory=dict)

    # Body
    sdp: Optional[SDPInfo] = None
    body: str = ""

    # Raw data
    raw: str = ""
    raw_bytes: bytes = b""

    # Dialog correlation
    call_id: str = ""
    from_uri: str = ""
    from_tag: str = ""
    from_display: str = ""
    to_uri: str = ""
    to_tag: str = ""
    to_display: str = ""
    cseq: str = ""
    cseq_method: str = ""
    via_branch: str = ""
    contact: str = ""
    user_agent: str = ""
    dialog_id: str = ""

    def __post_init__(self):
        if not self.message_id:
            ts = str(time.monotonic_ns())
            self.message_id = hashlib.sha256(ts.encode()).hexdigest()[:12]

    def get_header(self, name: str) -> Optional[str]:
        """Get header value by name (case-insensitive)."""
        key = name.lower()
        for h_name, h in self.headers.items():
            if h_name.lower() == key:
                return h.value
        return None

    def get_header_values(self, name: str) -> list[str]:
        key = name.lower()
        for h_name, h in self.headers.items():
            if h_name.lower() == key:
                return h.values
        return []

    @property
    def summary(self) -> str:
        if self.message_type == SIPMessageType.REQUEST:
            return f"{self.method} {self.request_uri}"
        return f"{self.status_code} {self.reason_phrase}"

    @property
    def short_label(self) -> str:
        """Short label for ladder diagrams."""
        if self.message_type == SIPMessageType.REQUEST:
            return self.method
        return f"{self.status_code} {self.reason_phrase}"

    @property
    def is_request(self) -> bool:
        return self.message_type == SIPMessageType.REQUEST

    def hex_dump(self, bytes_per_line: int = 16) -> str:
        """Generate a hex dump of the raw message."""
        data = self.raw_bytes or self.raw.encode("utf-8", errors="replace")
        return _hex_dump(data, bytes_per_line)

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "timestamp": self.timestamp.isoformat() if isinstance(self.timestamp, datetime.datetime) else self.timestamp,
            "direction": self.direction.value,
            "source": f"{self.source_ip}:{self.source_port}",
            "destination": f"{self.dest_ip}:{self.dest_port}",
            "transport": self.transport,
            "message_type": self.message_type.value,
            "method": self.method,
            "request_uri": self.request_uri,
            "status_code": self.status_code,
            "reason_phrase": self.reason_phrase,
            "summary": self.summary,
            "call_id": self.call_id,
            "from": {"uri": self.from_uri, "tag": self.from_tag, "display": self.from_display},
            "to": {"uri": self.to_uri, "tag": self.to_tag, "display": self.to_display},
            "cseq": self.cseq,
            "cseq_method": self.cseq_method,
            "via_branch": self.via_branch,
            "contact": self.contact,
            "user_agent": self.user_agent,
            "dialog_id": self.dialog_id,
            "headers": {n: h.to_dict() for n, h in self.headers.items()},
            "sdp": self.sdp.to_dict() if self.sdp else None,
            "has_sdp": self.sdp is not None,
            "body_length": len(self.body),
        }


# ─── Hex Dump ─────────────────────────────────────────────────────────────────

def _hex_dump(data: bytes, width: int = 16) -> str:
    """Generate a hex dump string like tcpdump/wireshark."""
    lines: list[str] = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:08x}  {hex_part:<{width * 3}} |{ascii_part}|")
    return "\n".join(lines)


# ─── Name-Addr Parser ────────────────────────────────────────────────────────

def _parse_name_addr(value: str) -> tuple[str, str, str]:
    """Parse a From/To header into (uri, tag, display_name)."""
    display = ""
    uri = ""
    tag = ""

    display_match = re.match(r'"([^"]*)"', value)
    if display_match:
        display = display_match.group(1)

    uri_match = re.search(r"<([^>]+)>", value)
    if uri_match:
        uri = uri_match.group(1)
    elif not display:
        uri = value.split(";")[0].strip()

    tag_match = re.search(r"tag=([^;\s>]+)", value)
    if tag_match:
        tag = tag_match.group(1)

    return uri, tag, display


# ─── SIP Parser ──────────────────────────────────────────────────────────────

class SIPParser:
    """Parses raw SIP message text into structured SIPMessage objects."""

    @classmethod
    def parse(cls, raw: str | bytes, **kwargs: Any) -> SIPMessage:
        """Parse a raw SIP message string or bytes."""
        if isinstance(raw, bytes):
            raw_bytes = raw
            raw_str = raw.decode("utf-8", errors="replace")
        else:
            raw_str = raw
            raw_bytes = raw.encode("utf-8", errors="replace")

        msg = SIPMessage(raw=raw_str, raw_bytes=raw_bytes, **kwargs)

        # Split headers from body
        parts = raw_str.split("\r\n\r\n", 1)
        if len(parts) == 1:
            parts = raw_str.split("\n\n", 1)
        header_block = parts[0]
        body = parts[1] if len(parts) > 1 else ""

        lines = header_block.replace("\r\n", "\n").split("\n")
        if not lines:
            return msg

        # Parse start line
        cls._parse_start_line(msg, lines[0].strip())

        # Parse headers
        for line in lines[1:]:
            line = line.strip()
            if not line:
                continue
            match = _HEADER_RE.match(line)
            if match:
                name = match.group(1).strip()
                value = match.group(2).strip()
                # Expand compact headers
                name = _COMPACT_HEADERS.get(name.lower(), name)
                if name in msg.headers:
                    msg.headers[name].values.append(value)
                else:
                    msg.headers[name] = SIPHeader(name=name, values=[value])

        # Extract dialog identifiers
        cls._extract_dialog_info(msg)

        # Parse body (SDP)
        msg.body = body
        content_type = msg.get_header("Content-Type") or ""
        if "application/sdp" in content_type.lower() and body.strip():
            msg.sdp = cls._parse_sdp(body)

        return msg

    @staticmethod
    def _parse_start_line(msg: SIPMessage, line: str) -> None:
        if line.startswith("SIP/"):
            msg.message_type = SIPMessageType.RESPONSE
            parts = line.split(None, 2)
            msg.sip_version = parts[0] if len(parts) > 0 else "SIP/2.0"
            try:
                msg.status_code = int(parts[1]) if len(parts) > 1 else 0
            except ValueError:
                msg.status_code = 0
            msg.reason_phrase = parts[2] if len(parts) > 2 else ""
        else:
            msg.message_type = SIPMessageType.REQUEST
            parts = line.split(None, 2)
            msg.method = parts[0] if len(parts) > 0 else ""
            msg.request_uri = parts[1] if len(parts) > 1 else ""
            msg.sip_version = parts[2] if len(parts) > 2 else "SIP/2.0"

    @staticmethod
    def _extract_dialog_info(msg: SIPMessage) -> None:
        msg.call_id = msg.get_header("Call-ID") or ""

        from_val = msg.get_header("From") or ""
        msg.from_uri, msg.from_tag, msg.from_display = _parse_name_addr(from_val)

        to_val = msg.get_header("To") or ""
        msg.to_uri, msg.to_tag, msg.to_display = _parse_name_addr(to_val)

        cseq = msg.get_header("CSeq") or ""
        msg.cseq = cseq
        cseq_parts = cseq.split()
        if len(cseq_parts) >= 2:
            msg.cseq_method = cseq_parts[1]

        via_val = msg.get_header("Via") or ""
        branch_match = re.search(r";branch=([^\s;>]+)", via_val)
        msg.via_branch = branch_match.group(1) if branch_match else ""

        msg.contact = msg.get_header("Contact") or ""
        msg.user_agent = msg.get_header("User-Agent") or msg.get_header("Server") or ""

        msg.dialog_id = f"{msg.call_id}|{msg.from_tag}|{msg.to_tag}"

    @staticmethod
    def _parse_sdp(body: str) -> SDPInfo:
        sdp = SDPInfo(raw=body)
        for line in body.strip().replace("\r\n", "\n").split("\n"):
            line = line.strip()
            if not line or len(line) < 2 or line[1] != "=":
                continue
            field_type = line[0]
            value = line[2:]

            if field_type == "v":
                sdp.version = value
            elif field_type == "o":
                sdp.origin = value
            elif field_type == "s":
                sdp.session_name = value
            elif field_type == "c":
                sdp.connection = value
            elif field_type == "m":
                sdp.media_lines.append(value)
            elif field_type == "a":
                attr_parts = value.split(":", 1)
                attr_name = attr_parts[0]
                attr_value = attr_parts[1] if len(attr_parts) > 1 else ""
                sdp.attributes.setdefault(attr_name, []).append(attr_value)

        return sdp


# ─── Backward-Compat Aliases ─────────────────────────────────────────────────

# Keep the old names for backward compatibility with existing code
ParsedSIPMessage = SIPMessage


def parse_sip_message(raw: str, timestamp: float = 0.0) -> SIPMessage:
    """Parse a raw SIP message string into a structured object.
    Backward-compatible wrapper around SIPParser.parse().
    """
    ts = datetime.datetime.utcfromtimestamp(timestamp) if timestamp else datetime.datetime.utcnow()
    return SIPParser.parse(raw, timestamp=ts)


# ─── Message Filter ──────────────────────────────────────────────────────────

@dataclass
class SIPFilter:
    """Filter criteria for SIP message search."""
    method: Optional[str] = None
    status_code: Optional[int] = None
    call_id: Optional[str] = None
    direction: Optional[SIPDirection] = None
    source_ip: Optional[str] = None
    dest_ip: Optional[str] = None
    text_contains: Optional[str] = None
    after: Optional[datetime.datetime] = None
    before: Optional[datetime.datetime] = None
    has_sdp: Optional[bool] = None
    limit: int = 100

    def matches(self, msg: SIPMessage) -> bool:
        if self.method and msg.method != self.method:
            return False
        if self.status_code is not None and msg.status_code != self.status_code:
            return False
        if self.call_id and msg.call_id != self.call_id:
            return False
        if self.direction and msg.direction != self.direction:
            return False
        if self.source_ip and msg.source_ip != self.source_ip:
            return False
        if self.dest_ip and msg.dest_ip != self.dest_ip:
            return False
        if self.text_contains and self.text_contains.lower() not in msg.raw.lower():
            return False
        if self.after and msg.timestamp < self.after:
            return False
        if self.before and msg.timestamp > self.before:
            return False
        if self.has_sdp is not None and (msg.sdp is not None) != self.has_sdp:
            return False
        return True

    def to_dict(self) -> dict:
        return {k: v for k, v in {
            "method": self.method,
            "status_code": self.status_code,
            "call_id": self.call_id,
            "direction": self.direction.value if self.direction else None,
            "source_ip": self.source_ip,
            "dest_ip": self.dest_ip,
            "text_contains": self.text_contains,
            "after": self.after.isoformat() if self.after else None,
            "before": self.before.isoformat() if self.before else None,
            "has_sdp": self.has_sdp,
            "limit": self.limit,
        }.items() if v is not None}


# ─── Ladder Diagram Generator ────────────────────────────────────────────────

class LadderDiagram:
    """Generates text-based SIP call flow ladder diagrams."""

    def __init__(self, column_width: int = 30, max_label_len: int = 24):
        self._column_width = column_width
        self._max_label_len = max_label_len

    def generate(self, messages: list[SIPMessage]) -> str:
        """Generate a ladder diagram from a list of SIP messages."""
        if not messages:
            return "(no messages)"

        # Identify unique endpoints
        endpoints: list[str] = []
        seen: set[str] = set()
        for msg in messages:
            src = f"{msg.source_ip}:{msg.source_port}" if msg.source_ip else "local"
            dst = f"{msg.dest_ip}:{msg.dest_port}" if msg.dest_ip else "remote"
            for ep in (src, dst):
                if ep not in seen:
                    seen.add(ep)
                    endpoints.append(ep)

        if len(endpoints) < 2:
            endpoints = ["local", "remote"]

        col_w = self._column_width

        lines: list[str] = []

        # Header
        header_parts: list[str] = []
        for ep in endpoints:
            label = ep if len(ep) <= col_w - 2 else ep[:col_w - 5] + "..."
            header_parts.append(label.center(col_w))
        ts_pad = " " * 14  # timestamp column width
        lines.append(f"{ts_pad}{(''.join(header_parts))}")

        # Column markers
        marker_parts: list[str] = []
        for _ in endpoints:
            marker_parts.append("|".center(col_w))
        separator = f"{ts_pad}{''.join(marker_parts)}"
        lines.append(separator)

        # Messages
        for msg in messages:
            src = f"{msg.source_ip}:{msg.source_port}" if msg.source_ip else "local"
            dst = f"{msg.dest_ip}:{msg.dest_port}" if msg.dest_ip else "remote"

            src_idx = endpoints.index(src) if src in endpoints else 0
            dst_idx = endpoints.index(dst) if dst in endpoints else (len(endpoints) - 1)

            label = msg.short_label
            if len(label) > self._max_label_len:
                label = label[:self._max_label_len - 3] + "..."

            if isinstance(msg.timestamp, datetime.datetime):
                ts_str = msg.timestamp.strftime("%H:%M:%S.%f")[:-3]
            else:
                ts_str = time.strftime("%H:%M:%S", time.localtime(msg.timestamp))

            lines.append(self._draw_arrow(endpoints, src_idx, dst_idx, label, ts_str))
            # SDP annotation
            if msg.sdp:
                sdp_note = f"  SDP: port={msg.sdp.media_port}, codecs={msg.sdp.codecs or msg.sdp.codec_ids}"
                lines.append(f"{ts_pad}{''.join(marker_parts)}")
                lines.append(f"{' ' * 14}{sdp_note}")
            lines.append(separator)

        return "\n".join(lines)

    def _draw_arrow(
        self,
        endpoints: list[str],
        src_idx: int,
        dst_idx: int,
        label: str,
        timestamp: str,
    ) -> str:
        col_w = self._column_width
        num_cols = len(endpoints)
        total = col_w * num_cols
        row = list(" " * total)

        # Draw vertical bars at each endpoint position
        for i in range(num_cols):
            center = i * col_w + col_w // 2
            if center < len(row):
                row[center] = "|"

        left = min(src_idx, dst_idx)
        right = max(src_idx, dst_idx)
        left_pos = left * col_w + col_w // 2
        right_pos = right * col_w + col_w // 2

        going_right = src_idx < dst_idx

        # Draw arrow line
        for i in range(left_pos + 1, right_pos):
            if i < len(row):
                row[i] = "-"

        # Arrow head
        if going_right:
            if right_pos < len(row):
                row[right_pos] = ">"
        else:
            if left_pos < len(row):
                row[left_pos] = "<"

        # Place label centered on the arrow
        mid = (left_pos + right_pos) // 2
        label_start = mid - len(label) // 2
        for i, ch in enumerate(label):
            pos = label_start + i
            if 0 <= pos < len(row):
                row[pos] = ch

        ts_padded = f"{timestamp:<14}"
        return f"{ts_padded}{''.join(row)}"


# ─── SIP Debug Store / Debugger ──────────────────────────────────────────────

class SIPDebugger:
    """
    Central store for captured SIP messages.
    Maintains a global ring buffer and per-dialog message lists.
    Provides search, filtering, ladder diagrams, and hex dumps.
    """

    def __init__(
        self,
        max_messages: int = 5000,
        max_per_dialog: int = 200,
        max_dialogs: int = 500,
    ):
        self._max_messages = max_messages
        self._max_per_dialog = max_per_dialog
        self._max_dialogs = max_dialogs

        self._messages: deque[SIPMessage] = deque(maxlen=max_messages)
        self._dialogs: OrderedDict[str, deque[SIPMessage]] = OrderedDict()
        self._lock = threading.Lock()
        self._listeners: list[Callable[[SIPMessage], Any]] = []
        self._parser = SIPParser()
        self._diagram = LadderDiagram()
        self._capture_enabled = True

        self._counters = {
            "total_captured": 0,
            "requests": 0,
            "responses": 0,
            "sent": 0,
            "received": 0,
        }

        logger.info(
            "SIP debugger initialized (max_msgs=%d, max_dialogs=%d)",
            max_messages, max_dialogs,
        )

    # ─── Capture ──────────────────────────────────────────────────────────

    def capture(
        self,
        raw_message: str | bytes,
        timestamp: float = 0.0,
        direction: SIPDirection = SIPDirection.RECEIVED,
        source_ip: str = "",
        source_port: int = 0,
        dest_ip: str = "",
        dest_port: int = 0,
        transport: str = "udp",
    ) -> SIPMessage:
        """Parse and store a captured SIP message."""
        if not self._capture_enabled:
            return SIPMessage()

        ts = datetime.datetime.utcfromtimestamp(timestamp) if timestamp else datetime.datetime.utcnow()

        msg = self._parser.parse(
            raw_message,
            timestamp=ts,
            direction=direction,
            source_ip=source_ip,
            source_port=source_port,
            dest_ip=dest_ip,
            dest_port=dest_port,
            transport=transport,
        )

        with self._lock:
            self._messages.append(msg)
            self._counters["total_captured"] += 1
            if msg.message_type == SIPMessageType.REQUEST:
                self._counters["requests"] += 1
            else:
                self._counters["responses"] += 1
            if msg.direction == SIPDirection.SENT:
                self._counters["sent"] += 1
            else:
                self._counters["received"] += 1

            # Store in dialog
            dialog_key = msg.call_id or msg.dialog_id
            if dialog_key:
                if dialog_key not in self._dialogs:
                    if len(self._dialogs) >= self._max_dialogs:
                        self._dialogs.popitem(last=False)
                    self._dialogs[dialog_key] = deque(maxlen=self._max_per_dialog)
                self._dialogs[dialog_key].append(msg)

        # Notify listeners
        for listener in self._listeners:
            try:
                listener(msg)
            except Exception:
                pass

        logger.debug(
            "SIP captured: %s %s %s:%d -> %s:%d",
            direction.value, msg.summary, source_ip, source_port, dest_ip, dest_port,
        )
        return msg

    def capture_raw_bytes(self, data: bytes, **kwargs: Any) -> Optional[SIPMessage]:
        """Attempt to capture bytes as SIP. Returns None if not valid SIP."""
        try:
            text = data.decode("utf-8", errors="replace")
        except Exception:
            return None
        first_line = text.split("\n", 1)[0].strip()
        if not (first_line.startswith("SIP/") or
                any(first_line.startswith(m + " ") for m in _SIP_METHODS)):
            return None
        return self.capture(data, **kwargs)

    # ─── Enable / Disable ─────────────────────────────────────────────────

    def enable_capture(self) -> None:
        self._capture_enabled = True

    def disable_capture(self) -> None:
        self._capture_enabled = False

    # ─── Query ────────────────────────────────────────────────────────────

    def search(self, filt: SIPFilter) -> list[dict]:
        """Search messages with a filter."""
        with self._lock:
            messages = list(self._messages)
        results: list[dict] = []
        for msg in reversed(messages):
            if filt.matches(msg):
                results.append(msg.to_dict())
                if len(results) >= filt.limit:
                    break
        return results

    def get_messages(
        self,
        limit: int = 50,
        method: str = "",
        call_id: str = "",
        since: float = 0.0,
    ) -> list[dict]:
        """Search/filter captured messages (backward-compatible API)."""
        with self._lock:
            msgs = list(self._messages)

        if call_id:
            msgs = [m for m in msgs if m.call_id == call_id]
        if method:
            msgs = [m for m in msgs if m.method == method or str(m.status_code) == method]
        if since > 0:
            cutoff = datetime.datetime.utcfromtimestamp(since)
            msgs = [m for m in msgs if isinstance(m.timestamp, datetime.datetime) and m.timestamp >= cutoff]

        return [m.to_dict() for m in msgs[-limit:]]

    def get_recent(self, limit: int = 50) -> list[dict]:
        with self._lock:
            msgs = list(self._messages)
        return [m.to_dict() for m in msgs[-limit:]]

    def get_dialog(self, call_id: str) -> list[dict]:
        """Get all messages for a specific dialog."""
        with self._lock:
            msgs = list(self._dialogs.get(call_id, []))
        return [m.to_dict() for m in msgs]

    def get_message(self, message_id: str) -> Optional[dict]:
        with self._lock:
            for msg in self._messages:
                if msg.message_id == message_id:
                    return msg.to_dict()
        return None

    def get_message_raw(self, message_id: str) -> Optional[str]:
        with self._lock:
            for msg in self._messages:
                if msg.message_id == message_id:
                    return msg.raw
        return None

    def get_message_hex(self, message_id: str) -> Optional[str]:
        with self._lock:
            for msg in self._messages:
                if msg.message_id == message_id:
                    return msg.hex_dump()
        return None

    # ─── Dialog Views ─────────────────────────────────────────────────────

    def get_dialog_ladder(self, call_id: str) -> str:
        """Generate a text-based ladder diagram for a dialog."""
        with self._lock:
            dialog = self._dialogs.get(call_id)
            if not dialog:
                return f"No messages for Call-ID: {call_id}"
            messages = list(dialog)
        return self._diagram.generate(messages)

    def get_active_dialogs(self) -> list[dict]:
        """List all tracked dialogs."""
        with self._lock:
            result: list[dict] = []
            for call_id, msgs in self._dialogs.items():
                if not msgs:
                    continue
                first = msgs[0]
                last = msgs[-1]
                methods: set[str] = set()
                for m in msgs:
                    if m.is_request:
                        methods.add(m.method)

                result.append({
                    "call_id": call_id,
                    "message_count": len(msgs),
                    "first_seen": first.timestamp.isoformat() if isinstance(first.timestamp, datetime.datetime) else first.timestamp,
                    "last_seen": last.timestamp.isoformat() if isinstance(last.timestamp, datetime.datetime) else last.timestamp,
                    "methods": sorted(methods),
                    "from": first.from_uri,
                    "to": first.to_uri,
                    "last_message": last.summary,
                })
        return result

    def list_dialogs(self, limit: int = 50) -> list[dict]:
        """Alias for get_active_dialogs with limit."""
        dialogs = self.get_active_dialogs()
        return dialogs[-limit:]

    # ─── Listeners ────────────────────────────────────────────────────────

    def add_listener(self, callback: Callable[[SIPMessage], Any]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[SIPMessage], Any]) -> None:
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    # ─── Maintenance ──────────────────────────────────────────────────────

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()
            self._dialogs.clear()
            self._counters = {k: 0 for k in self._counters}
        logger.info("SIP debugger cleared")

    def clear_dialog(self, call_id: str) -> bool:
        with self._lock:
            return self._dialogs.pop(call_id, None) is not None

    # ─── Serialization ────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            total = len(self._messages)
            dialogs = len(self._dialogs)
            methods: dict[str, int] = defaultdict(int)
            codes: dict[int, int] = defaultdict(int)
            for m in self._messages:
                if m.is_request:
                    methods[m.method] += 1
                else:
                    codes[m.status_code] += 1

        return {
            "total_messages": total,
            "active_dialogs": dialogs,
            "capture_enabled": self._capture_enabled,
            "methods": dict(methods),
            "response_codes": {str(k): v for k, v in codes.items()},
        }

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "total_captured": self._counters["total_captured"],
                "requests": self._counters["requests"],
                "responses": self._counters["responses"],
                "sent": self._counters["sent"],
                "received": self._counters["received"],
                "buffer_size": len(self._messages),
                "buffer_capacity": self._max_messages,
                "active_dialogs": len(self._dialogs),
                "max_dialogs": self._max_dialogs,
                "capture_enabled": self._capture_enabled,
            }


# Alias for the new name
SIPDebugStore = SIPDebugger
