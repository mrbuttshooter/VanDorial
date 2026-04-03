"""
GenCall - SIP Message Debugger

Real-time SIP message capture, parsing, and analysis:
  - Parse SIP headers, SDP bodies, Via chains
  - Store message history per dialog
  - Generate text-based call flow ladders
  - Search/filter messages
  - Hex dump for raw analysis
"""

import re
import time
import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("gencall.sip_debug")


@dataclass
class ParsedSIPMessage:
    """Fully parsed SIP message."""
    timestamp: float = 0.0
    raw: str = ""

    # Request line
    is_request: bool = True
    method: str = ""
    request_uri: str = ""

    # Response line
    status_code: int = 0
    reason_phrase: str = ""

    # Core headers
    call_id: str = ""
    cseq: str = ""
    cseq_method: str = ""
    from_uri: str = ""
    from_tag: str = ""
    from_display: str = ""
    to_uri: str = ""
    to_tag: str = ""
    to_display: str = ""
    via: list[str] = field(default_factory=list)
    contact: str = ""
    content_type: str = ""
    content_length: int = 0
    max_forwards: int = 70
    user_agent: str = ""
    server: str = ""
    allow: list[str] = field(default_factory=list)

    # Auth
    www_authenticate: str = ""
    authorization: str = ""
    proxy_authenticate: str = ""
    proxy_authorization: str = ""

    # SDP
    has_sdp: bool = False
    sdp_origin: str = ""
    sdp_connection: str = ""
    sdp_media: list[str] = field(default_factory=list)
    sdp_audio_port: int = 0
    sdp_codecs: list[int] = field(default_factory=list)

    # All headers as dict
    headers: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    @property
    def summary(self) -> str:
        if self.is_request:
            return f"{self.method} {self.request_uri}"
        return f"{self.status_code} {self.reason_phrase}"

    @property
    def dialog_id(self) -> str:
        return f"{self.call_id}|{self.from_tag}|{self.to_tag}"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "summary": self.summary,
            "is_request": self.is_request,
            "method": self.method,
            "status_code": self.status_code,
            "call_id": self.call_id,
            "cseq": self.cseq,
            "from": {"uri": self.from_uri, "tag": self.from_tag, "display": self.from_display},
            "to": {"uri": self.to_uri, "tag": self.to_tag, "display": self.to_display},
            "via_count": len(self.via),
            "contact": self.contact,
            "has_sdp": self.has_sdp,
            "sdp_audio_port": self.sdp_audio_port,
            "sdp_codecs": self.sdp_codecs,
            "user_agent": self.user_agent or self.server,
        }

    def hex_dump(self, bytes_per_line: int = 16) -> str:
        """Generate a hex dump of the raw message."""
        raw_bytes = self.raw.encode("utf-8", errors="replace")
        lines = []
        for offset in range(0, len(raw_bytes), bytes_per_line):
            chunk = raw_bytes[offset:offset + bytes_per_line]
            hex_part = " ".join(f"{b:02x}" for b in chunk)
            ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            lines.append(f"{offset:08x}  {hex_part:<{bytes_per_line * 3}} |{ascii_part}|")
        return "\n".join(lines)


def parse_sip_message(raw: str, timestamp: float = 0.0) -> ParsedSIPMessage:
    """Parse a raw SIP message string into a structured object."""
    msg = ParsedSIPMessage(timestamp=timestamp or time.time(), raw=raw)

    parts = raw.split("\r\n\r\n", 1)
    header_block = parts[0]
    body = parts[1] if len(parts) > 1 else ""

    lines = header_block.split("\r\n")
    if not lines:
        return msg

    # Parse first line
    first = lines[0]
    if first.startswith("SIP/"):
        msg.is_request = False
        match = re.match(r"SIP/\d\.\d\s+(\d+)\s+(.*)", first)
        if match:
            msg.status_code = int(match.group(1))
            msg.reason_phrase = match.group(2)
    else:
        msg.is_request = True
        match = re.match(r"(\w+)\s+(\S+)\s+SIP/", first)
        if match:
            msg.method = match.group(1)
            msg.request_uri = match.group(2)

    # Parse headers
    for line in lines[1:]:
        if not line or not ":" in line:
            continue
        name, _, value = line.partition(":")
        name = name.strip()
        value = value.strip()
        name_lower = name.lower()
        msg.headers[name_lower].append(value)

        if name_lower in ("call-id", "i"):
            msg.call_id = value
        elif name_lower == "cseq":
            msg.cseq = value
            cseq_parts = value.split()
            if len(cseq_parts) >= 2:
                msg.cseq_method = cseq_parts[1]
        elif name_lower in ("from", "f"):
            msg.from_uri, msg.from_tag, msg.from_display = _parse_name_addr(value)
        elif name_lower in ("to", "t"):
            msg.to_uri, msg.to_tag, msg.to_display = _parse_name_addr(value)
        elif name_lower in ("via", "v"):
            msg.via.append(value)
        elif name_lower == "contact":
            msg.contact = value
        elif name_lower == "content-type":
            msg.content_type = value
        elif name_lower == "content-length":
            msg.content_length = int(value) if value.isdigit() else 0
        elif name_lower == "max-forwards":
            msg.max_forwards = int(value) if value.isdigit() else 70
        elif name_lower == "user-agent":
            msg.user_agent = value
        elif name_lower == "server":
            msg.server = value
        elif name_lower == "allow":
            msg.allow = [m.strip() for m in value.split(",")]
        elif name_lower == "www-authenticate":
            msg.www_authenticate = value
        elif name_lower == "authorization":
            msg.authorization = value
        elif name_lower == "proxy-authenticate":
            msg.proxy_authenticate = value
        elif name_lower == "proxy-authorization":
            msg.proxy_authorization = value

    # Parse SDP body
    if body and "application/sdp" in msg.content_type.lower():
        msg.has_sdp = True
        _parse_sdp(msg, body)

    return msg


def _parse_name_addr(value: str) -> tuple[str, str, str]:
    """Parse a From/To header into (uri, tag, display_name)."""
    display = ""
    uri = ""
    tag = ""

    # Extract display name
    display_match = re.match(r'"([^"]*)"', value)
    if display_match:
        display = display_match.group(1)

    # Extract URI
    uri_match = re.search(r"<([^>]+)>", value)
    if uri_match:
        uri = uri_match.group(1)
    elif not display:
        uri = value.split(";")[0].strip()

    # Extract tag
    tag_match = re.search(r"tag=([^;\s>]+)", value)
    if tag_match:
        tag = tag_match.group(1)

    return uri, tag, display


def _parse_sdp(msg: ParsedSIPMessage, body: str):
    """Parse SDP body for media info."""
    for line in body.split("\r\n"):
        if not line or len(line) < 2 or line[1] != "=":
            continue
        field_type = line[0]
        field_value = line[2:]

        if field_type == "o":
            msg.sdp_origin = field_value
        elif field_type == "c":
            msg.sdp_connection = field_value
        elif field_type == "m":
            msg.sdp_media.append(field_value)
            # Parse m=audio port RTP/AVP codec_list
            m_match = re.match(r"audio\s+(\d+)\s+RTP/AVP\s+([\d\s]+)", field_value)
            if m_match:
                msg.sdp_audio_port = int(m_match.group(1))
                msg.sdp_codecs = [int(c) for c in m_match.group(2).split()]


class SIPDebugger:
    """
    Real-time SIP message capture and analysis.
    Stores messages per dialog with search/filter capabilities.
    """

    def __init__(self, max_messages: int = 5000, max_per_dialog: int = 200):
        self._messages: deque[ParsedSIPMessage] = deque(maxlen=max_messages)
        self._dialogs: dict[str, deque[ParsedSIPMessage]] = defaultdict(
            lambda: deque(maxlen=max_per_dialog)
        )
        self._lock = threading.Lock()
        self._listeners: list = []
        self._capture_enabled = True

    def capture(self, raw_message: str, timestamp: float = 0.0):
        """Capture and parse a SIP message."""
        if not self._capture_enabled:
            return

        msg = parse_sip_message(raw_message, timestamp)

        with self._lock:
            self._messages.append(msg)
            if msg.call_id:
                self._dialogs[msg.call_id].append(msg)

        for listener in self._listeners:
            try:
                listener(msg)
            except Exception:
                pass

        return msg

    def add_listener(self, callback):
        self._listeners.append(callback)

    def enable_capture(self):
        self._capture_enabled = True

    def disable_capture(self):
        self._capture_enabled = False

    def get_messages(self, limit: int = 50, method: str = "",
                     call_id: str = "", since: float = 0.0) -> list[dict]:
        """Search/filter captured messages."""
        with self._lock:
            msgs = list(self._messages)

        if call_id:
            msgs = [m for m in msgs if m.call_id == call_id]
        if method:
            msgs = [m for m in msgs if m.method == method or str(m.status_code) == method]
        if since > 0:
            msgs = [m for m in msgs if m.timestamp >= since]

        return [m.to_dict() for m in msgs[-limit:]]

    def get_dialog(self, call_id: str) -> list[dict]:
        """Get all messages for a specific dialog."""
        with self._lock:
            msgs = list(self._dialogs.get(call_id, []))
        return [m.to_dict() for m in msgs]

    def get_dialog_ladder(self, call_id: str) -> str:
        """Generate a ladder diagram for a dialog."""
        with self._lock:
            msgs = list(self._dialogs.get(call_id, []))

        if not msgs:
            return f"No messages for Call-ID: {call_id}"

        lines = [
            f"Call-ID: {call_id}",
            f"Messages: {len(msgs)}",
            "",
        ]

        for msg in msgs:
            ts = time.strftime("%H:%M:%S", time.localtime(msg.timestamp))
            direction = ">>>" if msg.is_request else "<<<"
            lines.append(f"  {ts}  {direction}  {msg.summary}")
            if msg.has_sdp:
                lines.append(f"           SDP: port={msg.sdp_audio_port}, codecs={msg.sdp_codecs}")

        return "\n".join(lines)

    def get_active_dialogs(self) -> list[dict]:
        """List all tracked dialogs."""
        with self._lock:
            result = []
            for call_id, msgs in self._dialogs.items():
                if not msgs:
                    continue
                first = msgs[0]
                last = msgs[-1]
                methods = set()
                for m in msgs:
                    if m.is_request:
                        methods.add(m.method)

                result.append({
                    "call_id": call_id,
                    "message_count": len(msgs),
                    "first_seen": first.timestamp,
                    "last_seen": last.timestamp,
                    "methods": list(methods),
                    "from": first.from_uri,
                    "to": first.to_uri,
                })

        return result

    def clear(self):
        """Clear all captured messages."""
        with self._lock:
            self._messages.clear()
            self._dialogs.clear()

    def stats(self) -> dict:
        with self._lock:
            total = len(self._messages)
            dialogs = len(self._dialogs)
            methods = defaultdict(int)
            codes = defaultdict(int)
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
            "response_codes": dict(codes),
        }
