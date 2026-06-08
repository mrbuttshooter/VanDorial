"""
GenCall Call Flow Recorder & Replayer.

Records complete SIP dialogs (all messages with precise timing) into a
portable JSON format that can be replayed against a target with
controllable speed.  Also supports diffing two recordings to compare
call flows.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("gencall.call_recorder")

_DEFAULT_LIBRARY_DIR = "/opt/gencall/recordings"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class RecordingState(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    STOPPED = "stopped"
    ERROR = "error"


class ReplayState(Enum):
    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"


class MessageDirection(Enum):
    SENT = "sent"
    RECEIVED = "received"


@dataclass
class RecordedMessage:
    """A single SIP message captured during recording."""
    index: int = 0
    timestamp_offset_ms: float = 0.0
    direction: MessageDirection = MessageDirection.SENT
    method: str = ""
    status_code: int = 0
    reason_phrase: str = ""
    call_id: str = ""
    cseq: str = ""
    raw: str = ""
    source_ip: str = ""
    source_port: int = 0
    dest_ip: str = ""
    dest_port: int = 0
    transport: str = "udp"
    has_sdp: bool = False
    sdp_media_port: int = 0
    sdp_codecs: list[str] = field(default_factory=list)
    size_bytes: int = 0

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "timestamp_offset_ms": round(self.timestamp_offset_ms, 3),
            "direction": self.direction.value,
            "method": self.method,
            "status_code": self.status_code,
            "reason_phrase": self.reason_phrase,
            "call_id": self.call_id,
            "cseq": self.cseq,
            "raw": self.raw,
            "source_ip": self.source_ip,
            "source_port": self.source_port,
            "dest_ip": self.dest_ip,
            "dest_port": self.dest_port,
            "transport": self.transport,
            "has_sdp": self.has_sdp,
            "sdp_media_port": self.sdp_media_port,
            "sdp_codecs": self.sdp_codecs,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RecordedMessage":
        return cls(
            index=data.get("index", 0),
            timestamp_offset_ms=data.get("timestamp_offset_ms", 0.0),
            direction=MessageDirection(data.get("direction", "sent")),
            method=data.get("method", ""),
            status_code=data.get("status_code", 0),
            reason_phrase=data.get("reason_phrase", ""),
            call_id=data.get("call_id", ""),
            cseq=data.get("cseq", ""),
            raw=data.get("raw", ""),
            source_ip=data.get("source_ip", ""),
            source_port=data.get("source_port", 0),
            dest_ip=data.get("dest_ip", ""),
            dest_port=data.get("dest_port", 0),
            transport=data.get("transport", "udp"),
            has_sdp=data.get("has_sdp", False),
            sdp_media_port=data.get("sdp_media_port", 0),
            sdp_codecs=data.get("sdp_codecs", []),
            size_bytes=data.get("size_bytes", 0),
        )


@dataclass
class RTPInfo:
    """RTP stream metadata recorded alongside a call."""
    codec: str = ""
    payload_type: int = 0
    ssrc: int = 0
    local_port: int = 0
    remote_port: int = 0
    remote_ip: str = ""
    packets_sent: int = 0
    packets_received: int = 0
    duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "codec": self.codec,
            "payload_type": self.payload_type,
            "ssrc": self.ssrc,
            "local_port": self.local_port,
            "remote_port": self.remote_port,
            "remote_ip": self.remote_ip,
            "packets_sent": self.packets_sent,
            "packets_received": self.packets_received,
            "duration_ms": round(self.duration_ms, 2),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RTPInfo":
        return cls(
            codec=data.get("codec", ""),
            payload_type=data.get("payload_type", 0),
            ssrc=data.get("ssrc", 0),
            local_port=data.get("local_port", 0),
            remote_port=data.get("remote_port", 0),
            remote_ip=data.get("remote_ip", ""),
            packets_sent=data.get("packets_sent", 0),
            packets_received=data.get("packets_received", 0),
            duration_ms=data.get("duration_ms", 0.0),
        )


@dataclass
class CallRecording:
    """A complete recorded SIP dialog."""
    recording_id: str = ""
    name: str = ""
    description: str = ""
    scenario_name: str = ""
    caller: str = ""
    callee: str = ""
    call_id: str = ""
    created_at: Optional[datetime.datetime] = None
    duration_ms: float = 0.0
    messages: list[RecordedMessage] = field(default_factory=list)
    rtp_streams: list[RTPInfo] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.recording_id:
            self.recording_id = uuid.uuid4().hex[:12]
        if self.created_at is None:
            self.created_at = datetime.datetime.utcnow()

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def sent_count(self) -> int:
        return sum(1 for m in self.messages if m.direction == MessageDirection.SENT)

    @property
    def received_count(self) -> int:
        return sum(1 for m in self.messages if m.direction == MessageDirection.RECEIVED)

    @property
    def methods_used(self) -> list[str]:
        return sorted({m.method for m in self.messages if m.method})

    def to_dict(self) -> dict:
        return {
            "recording_id": self.recording_id,
            "name": self.name,
            "description": self.description,
            "scenario_name": self.scenario_name,
            "caller": self.caller,
            "callee": self.callee,
            "call_id": self.call_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "duration_ms": round(self.duration_ms, 2),
            "message_count": self.message_count,
            "sent_count": self.sent_count,
            "received_count": self.received_count,
            "methods_used": self.methods_used,
            "messages": [m.to_dict() for m in self.messages],
            "rtp_streams": [r.to_dict() for r in self.rtp_streams],
            "tags": self.tags,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CallRecording":
        rec = cls(
            recording_id=data.get("recording_id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            scenario_name=data.get("scenario_name", ""),
            caller=data.get("caller", ""),
            callee=data.get("callee", ""),
            call_id=data.get("call_id", ""),
            duration_ms=data.get("duration_ms", 0.0),
            tags=data.get("tags", []),
            metadata=data.get("metadata", {}),
        )
        created = data.get("created_at")
        if created:
            try:
                rec.created_at = datetime.datetime.fromisoformat(created)
            except (ValueError, TypeError):
                pass
        rec.messages = [RecordedMessage.from_dict(m) for m in data.get("messages", [])]
        rec.rtp_streams = [RTPInfo.from_dict(r) for r in data.get("rtp_streams", [])]
        return rec

    def save(self, path: str) -> None:
        """Save recording to a JSON file."""
        with open(path, "w") as fp:
            json.dump(self.to_dict(), fp, indent=2, default=str)
        logger.info("Recording saved: %s (%d messages)", path, self.message_count)

    @classmethod
    def load(cls, path: str) -> "CallRecording":
        """Load recording from a JSON file."""
        with open(path, "r") as fp:
            data = json.load(fp)
        rec = cls.from_dict(data)
        logger.info("Recording loaded: %s (%d messages)", path, rec.message_count)
        return rec


# ---------------------------------------------------------------------------
# Diff result
# ---------------------------------------------------------------------------

@dataclass
class MessageDiff:
    """Difference between two messages at the same position."""
    index: int = 0
    field_name: str = ""
    recording_a: str = ""
    recording_b: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "field": self.field_name,
            "a": self.recording_a,
            "b": self.recording_b,
        }


@dataclass
class RecordingDiff:
    """Comparison result between two call recordings."""
    recording_a_id: str = ""
    recording_b_id: str = ""
    identical: bool = False
    message_count_a: int = 0
    message_count_b: int = 0
    timing_diff_ms: float = 0.0
    differences: list[MessageDiff] = field(default_factory=list)
    extra_in_a: list[int] = field(default_factory=list)
    extra_in_b: list[int] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "recording_a_id": self.recording_a_id,
            "recording_b_id": self.recording_b_id,
            "identical": self.identical,
            "message_count_a": self.message_count_a,
            "message_count_b": self.message_count_b,
            "timing_diff_ms": round(self.timing_diff_ms, 2),
            "differences": [d.to_dict() for d in self.differences],
            "extra_in_a": self.extra_in_a,
            "extra_in_b": self.extra_in_b,
            "summary": self.summary,
        }


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------

class CallRecorder:
    """
    Records a SIP dialog in real time.

    Feed messages via :meth:`record_message` as they are sent/received.
    Call :meth:`stop` to finalise and retrieve the recording.
    """

    def __init__(
        self,
        name: str = "",
        scenario_name: str = "",
        caller: str = "",
        callee: str = "",
    ) -> None:
        self._recording = CallRecording(
            name=name,
            scenario_name=scenario_name,
            caller=caller,
            callee=callee,
        )
        self._state = RecordingState.IDLE
        self._start_time: Optional[float] = None
        self._msg_counter = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> RecordingState:
        return self._state

    @property
    def recording(self) -> CallRecording:
        return self._recording

    def start(self) -> None:
        """Begin recording."""
        with self._lock:
            self._state = RecordingState.RECORDING
            self._start_time = time.monotonic()
            self._msg_counter = 0
        logger.info("Call recording started: %s", self._recording.recording_id)

    def record_message(
        self,
        raw: str,
        direction: MessageDirection = MessageDirection.SENT,
        source_ip: str = "",
        source_port: int = 0,
        dest_ip: str = "",
        dest_port: int = 0,
        transport: str = "udp",
    ) -> Optional[RecordedMessage]:
        """Add a SIP message to the recording."""
        with self._lock:
            if self._state != RecordingState.RECORDING:
                return None

            if self._start_time is None:
                self._start_time = time.monotonic()

            offset_ms = (time.monotonic() - self._start_time) * 1000.0
            self._msg_counter += 1

            msg = RecordedMessage(
                index=self._msg_counter,
                timestamp_offset_ms=offset_ms,
                direction=direction,
                raw=raw,
                source_ip=source_ip,
                source_port=source_port,
                dest_ip=dest_ip,
                dest_port=dest_port,
                transport=transport,
                size_bytes=len(raw.encode("utf-8", errors="replace")),
            )

            # Parse basic SIP info from the raw message
            self._parse_message_info(msg, raw)
            self._recording.messages.append(msg)

            if not self._recording.call_id and msg.call_id:
                self._recording.call_id = msg.call_id

            return msg

    def add_rtp_info(self, rtp_info: RTPInfo) -> None:
        """Attach RTP stream metadata to the recording."""
        self._recording.rtp_streams.append(rtp_info)

    def stop(self) -> CallRecording:
        """Stop recording and return the completed recording."""
        with self._lock:
            self._state = RecordingState.STOPPED
            if self._recording.messages:
                self._recording.duration_ms = (
                    self._recording.messages[-1].timestamp_offset_ms
                )
        logger.info(
            "Call recording stopped: %s (%d messages, %.1f ms)",
            self._recording.recording_id,
            self._recording.message_count,
            self._recording.duration_ms,
        )
        return self._recording

    @staticmethod
    def _parse_message_info(msg: RecordedMessage, raw: str) -> None:
        """Extract basic SIP fields from raw text."""
        lines = raw.replace("\r\n", "\n").split("\n")
        if not lines:
            return

        first = lines[0].strip()
        if first.startswith("SIP/"):
            parts = first.split(None, 2)
            if len(parts) >= 2:
                try:
                    msg.status_code = int(parts[1])
                except ValueError:
                    pass
                msg.reason_phrase = parts[2] if len(parts) > 2 else ""
        else:
            parts = first.split(None, 2)
            if parts:
                msg.method = parts[0]

        for line in lines[1:]:
            line = line.strip()
            if not line:
                break
            low = line.lower()
            if low.startswith("call-id:") or low.startswith("i:"):
                msg.call_id = line.split(":", 1)[1].strip()
            elif low.startswith("cseq:"):
                msg.cseq = line.split(":", 1)[1].strip()
            elif low.startswith("content-type:"):
                ct = line.split(":", 1)[1].strip().lower()
                if "application/sdp" in ct:
                    msg.has_sdp = True


# ---------------------------------------------------------------------------
# Replayer
# ---------------------------------------------------------------------------

class CallReplayer:
    """
    Replays a recorded call flow against a target.

    Sends recorded SENT messages (with timing preserved) to the target
    and receives responses.  Speed can be adjusted (e.g. 2.0 = double
    speed, 0.5 = half speed).
    """

    def __init__(
        self,
        recording: CallRecording,
        target_ip: str,
        target_port: int = 5060,
        local_ip: str = "0.0.0.0",
        local_port: int = 0,
        speed: float = 1.0,
        transport: str = "udp",
        on_message: Optional[Callable[[RecordedMessage, str], None]] = None,
    ) -> None:
        self._recording = recording
        self.target_ip = target_ip
        self.target_port = target_port
        self.local_ip = local_ip
        self.local_port = local_port
        self.speed = max(0.01, speed)
        self.transport = transport
        self._on_message = on_message
        self._state = ReplayState.IDLE
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused initially
        self._messages_sent = 0
        self._messages_received = 0
        self._errors: list[str] = []

    @property
    def state(self) -> ReplayState:
        return self._state

    @property
    def progress(self) -> dict:
        total_sent = sum(
            1 for m in self._recording.messages if m.direction == MessageDirection.SENT
        )
        return {
            "state": self._state.value,
            "messages_sent": self._messages_sent,
            "messages_received": self._messages_received,
            "total_to_send": total_sent,
            "speed": self.speed,
            "errors": self._errors[-10:],
        }

    def start(self) -> None:
        """Start replay in a background thread."""
        if self._state == ReplayState.PLAYING:
            return
        self._stop_event.clear()
        self._pause_event.set()
        self._state = ReplayState.PLAYING
        self._thread = threading.Thread(
            target=self._replay_loop, daemon=True, name="call-replayer",
        )
        self._thread.start()
        logger.info(
            "Replay started: %s -> %s:%d @ %.1fx speed",
            self._recording.recording_id, self.target_ip,
            self.target_port, self.speed,
        )

    def stop(self) -> None:
        """Stop replay."""
        self._stop_event.set()
        self._pause_event.set()

    def pause(self) -> None:
        """Pause replay."""
        self._pause_event.clear()
        self._state = ReplayState.PAUSED
        logger.info("Replay paused")

    def resume(self) -> None:
        """Resume paused replay."""
        self._pause_event.set()
        self._state = ReplayState.PLAYING
        logger.info("Replay resumed")

    def wait(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout=timeout)

    def _replay_loop(self) -> None:
        """Main replay loop - sends recorded SENT messages with original timing."""
        sock: Optional[socket.socket] = None
        try:
            family = socket.AF_INET6 if ":" in self.target_ip else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.settimeout(2.0)
            sock.bind((self.local_ip, self.local_port))

            sent_messages = [
                m for m in self._recording.messages
                if m.direction == MessageDirection.SENT
            ]
            if not sent_messages:
                self._state = ReplayState.COMPLETED
                return

            replay_start = time.monotonic()
            last_offset = 0.0

            for msg in sent_messages:
                if self._stop_event.is_set():
                    self._state = ReplayState.COMPLETED
                    return

                # Wait for pause to clear
                self._pause_event.wait()

                # Compute delay (adjusted for speed)
                delay_ms = (msg.timestamp_offset_ms - last_offset) / self.speed
                last_offset = msg.timestamp_offset_ms

                if delay_ms > 0:
                    deadline = time.monotonic() + delay_ms / 1000.0
                    while time.monotonic() < deadline:
                        if self._stop_event.is_set():
                            self._state = ReplayState.COMPLETED
                            return
                        remaining = deadline - time.monotonic()
                        time.sleep(min(remaining, 0.1))

                # Send the message
                try:
                    data = msg.raw.encode("utf-8", errors="replace")
                    sock.sendto(data, (self.target_ip, self.target_port))
                    self._messages_sent += 1

                    if self._on_message:
                        self._on_message(msg, "sent")

                    # Try to receive responses (non-blocking drain)
                    self._drain_responses(sock)

                except Exception as exc:
                    error_msg = f"Send error at msg {msg.index}: {exc}"
                    self._errors.append(error_msg)
                    logger.warning(error_msg)

            # Final drain of responses
            time.sleep(0.5)
            self._drain_responses(sock)
            self._state = ReplayState.COMPLETED

        except Exception as exc:
            self._state = ReplayState.ERROR
            self._errors.append(str(exc))
            logger.exception("Replay error")
        finally:
            if sock:
                sock.close()
            logger.info(
                "Replay finished: sent=%d received=%d errors=%d",
                self._messages_sent, self._messages_received, len(self._errors),
            )

    def _drain_responses(self, sock: socket.socket) -> None:
        """Non-blocking read of incoming responses."""
        sock.setblocking(False)
        try:
            while True:
                try:
                    data, addr = sock.recvfrom(65535)
                    self._messages_received += 1
                    if self._on_message:
                        resp_msg = RecordedMessage(
                            direction=MessageDirection.RECEIVED,
                            raw=data.decode("utf-8", errors="replace"),
                            source_ip=addr[0],
                            source_port=addr[1],
                        )
                        self._on_message(resp_msg, "received")
                except BlockingIOError:
                    break
                except Exception:
                    break
        finally:
            sock.setblocking(True)
            sock.settimeout(2.0)


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------

def diff_recordings(a: CallRecording, b: CallRecording) -> RecordingDiff:
    """
    Compare two call recordings and return the differences.

    Compares message-by-message: direction, method/status, and timing.
    """
    result = RecordingDiff(
        recording_a_id=a.recording_id,
        recording_b_id=b.recording_id,
        message_count_a=a.message_count,
        message_count_b=b.message_count,
    )

    # Compare common messages
    min_len = min(len(a.messages), len(b.messages))
    compare_fields = ["direction", "method", "status_code", "cseq"]

    for i in range(min_len):
        ma = a.messages[i]
        mb = b.messages[i]

        for fld in compare_fields:
            va = getattr(ma, fld)
            vb = getattr(mb, fld)
            if va != vb:
                result.differences.append(MessageDiff(
                    index=i,
                    field_name=fld,
                    recording_a=str(va),
                    recording_b=str(vb),
                ))

        # Timing difference
        timing_delta = abs(ma.timestamp_offset_ms - mb.timestamp_offset_ms)
        if timing_delta > 100:  # significant timing diff (>100ms)
            result.differences.append(MessageDiff(
                index=i,
                field_name="timing_offset_ms",
                recording_a=f"{ma.timestamp_offset_ms:.1f}",
                recording_b=f"{mb.timestamp_offset_ms:.1f}",
            ))

    # Extra messages
    if len(a.messages) > min_len:
        result.extra_in_a = list(range(min_len, len(a.messages)))
    if len(b.messages) > min_len:
        result.extra_in_b = list(range(min_len, len(b.messages)))

    # Overall timing difference
    if a.duration_ms > 0 and b.duration_ms > 0:
        result.timing_diff_ms = abs(a.duration_ms - b.duration_ms)

    result.identical = (
        len(result.differences) == 0
        and len(result.extra_in_a) == 0
        and len(result.extra_in_b) == 0
    )

    diff_count = len(result.differences)
    result.summary = (
        f"{'Identical' if result.identical else f'{diff_count} differences found'}, "
        f"A: {a.message_count} msgs, B: {b.message_count} msgs, "
        f"timing delta: {result.timing_diff_ms:.1f}ms"
    )
    logger.info("Recording diff: %s", result.summary)
    return result


# ---------------------------------------------------------------------------
# Recording Library
# ---------------------------------------------------------------------------

class RecordingLibrary:
    """
    Manages a directory of saved call recordings.

    Provides save, load, list, search, and delete operations.
    """

    def __init__(self, library_dir: str = _DEFAULT_LIBRARY_DIR) -> None:
        self._dir = library_dir
        os.makedirs(self._dir, exist_ok=True)
        logger.info("Recording library at: %s", self._dir)

    @property
    def directory(self) -> str:
        return self._dir

    def save(self, recording: CallRecording, filename: Optional[str] = None) -> str:
        """Save a recording and return the file path."""
        if filename is None:
            safe_name = (recording.name or recording.recording_id).replace(" ", "_")
            filename = f"{safe_name}_{recording.recording_id}.json"
        path = os.path.join(self._dir, filename)
        recording.save(path)
        return path

    def load(self, filename: str) -> CallRecording:
        """Load a recording by filename."""
        path = os.path.join(self._dir, filename)
        if not os.path.isfile(path):
            # Try as absolute path
            path = filename
        return CallRecording.load(path)

    def list_recordings(self) -> list[dict]:
        """List all recordings in the library (metadata only)."""
        recordings: list[dict] = []
        if not os.path.isdir(self._dir):
            return recordings

        for fname in sorted(os.listdir(self._dir)):
            if not fname.endswith(".json"):
                continue
            path = os.path.join(self._dir, fname)
            try:
                with open(path, "r") as fp:
                    data = json.load(fp)
                recordings.append({
                    "filename": fname,
                    "recording_id": data.get("recording_id", ""),
                    "name": data.get("name", ""),
                    "scenario_name": data.get("scenario_name", ""),
                    "caller": data.get("caller", ""),
                    "callee": data.get("callee", ""),
                    "message_count": data.get("message_count", 0),
                    "duration_ms": data.get("duration_ms", 0.0),
                    "created_at": data.get("created_at", ""),
                    "tags": data.get("tags", []),
                })
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug("Skipping invalid recording file %s: %s", fname, exc)
        return recordings

    def search(
        self,
        name: Optional[str] = None,
        scenario: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict]:
        """Search recordings by name, scenario, or tag."""
        all_recs = self.list_recordings()
        results: list[dict] = []
        for rec in all_recs:
            if name and name.lower() not in rec.get("name", "").lower():
                continue
            if scenario and scenario.lower() not in rec.get("scenario_name", "").lower():
                continue
            if tag and tag not in rec.get("tags", []):
                continue
            results.append(rec)
        return results

    def delete(self, filename: str) -> bool:
        """Delete a recording file."""
        path = os.path.join(self._dir, filename)
        if os.path.isfile(path):
            os.remove(path)
            logger.info("Recording deleted: %s", filename)
            return True
        return False

    def to_dict(self) -> dict:
        recordings = self.list_recordings()
        return {
            "library_dir": self._dir,
            "recording_count": len(recordings),
            "recordings": recordings,
        }
