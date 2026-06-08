"""
GenCall RTP Engine - Real-time Transport Protocol handling.
Supports audio file streaming, DTMF, tone generation, and pcap replay.
"""

import time
import threading
import queue
import random
import socket
import struct
import os
import math
import select
import logging
from typing import Optional

from gencall.core.config import Config

logger = logging.getLogger("gencall.rtp")

RECV_BUFFER = 1024
QUEUE_SIZE = 50


def is_ipv6(address: str) -> bool:
    return ":" in address


def rtp_header(version, extension, csrc_count, marker, payload_type,
               sequence_number, timestamp, ssrc) -> bytes:
    """Build an RTP packet header."""
    byte1 = bytes([((version & 0x3) << 6) | ((extension & 0x1) << 4) | (csrc_count & 0xF)])
    byte2 = bytes([((marker & 0x1) << 7) | (payload_type & 0x7F)])
    byte3 = struct.pack(">H", sequence_number & 0xFFFF)
    byte4 = struct.pack(">L", timestamp & 0xFFFFFFFF)
    byte5 = struct.pack(">L", ssrc & 0xFFFFFFFF)
    return byte1 + byte2 + byte3 + byte4 + byte5


def detect_codec(filename: str) -> tuple:
    """Detect codec from filename. Returns (payload_type, bytes_per_ms)."""
    name = filename.lower()
    if "g729" in name:
        return 18, 1  # G.729: 8kbps = 1 byte/ms
    elif "g711u" in name or "ulaw" in name or "pcmu" in name:
        return 0, 8   # G.711 u-law: 64kbps = 8 bytes/ms
    else:
        return 8, 8   # G.711 a-law: 64kbps = 8 bytes/ms


class BaseStreamer(threading.Thread):
    """Base class for RTP content streamers."""

    def __init__(self, name="BaseStreamer"):
        super().__init__(name=name, daemon=True)
        self.data_queue = queue.Queue(QUEUE_SIZE)
        self._running = True

    def stop(self):
        self._running = False

    def get_next_packet(self, timeout=1.0):
        return self.data_queue.get(True, timeout)

    def _open_media_file(self, filename: str):
        """Open a media file, checking GenCall media directory first."""
        config = Config()
        if not os.path.isabs(filename):
            media_path = os.path.join(config.media_path, filename)
            if os.path.exists(media_path):
                return open(media_path, "rb")
        return open(filename, "rb")


class AudioFileStreamer(BaseStreamer):
    """Streams audio from a raw codec file (g711a, g711u, g729)."""

    def __init__(self, filename: str, start_timestamp: int = 0,
                 ssrc: int = 0, ptime_ms: int = 20):
        super().__init__("AudioFileStreamer")
        self.filename = filename
        self.start_timestamp = start_timestamp
        self.ssrc = ssrc
        self.ptime_ms = ptime_ms
        self.timestamp_delta = 8 * ptime_ms  # samples per packet at 8kHz

        self.payload_type, bytes_per_ms = detect_codec(filename)
        self.payload_length = bytes_per_ms * ptime_ms

        logger.debug("AudioFileStreamer: file=%s, codec=%d, ptime=%dms, payload=%d bytes",
                      filename, self.payload_type, ptime_ms, self.payload_length)

    def run(self):
        seq = 0
        ts = self.start_timestamp
        time_play = 0

        try:
            audio_file = self._open_media_file(self.filename)
        except FileNotFoundError:
            logger.error("Audio file not found: %s", self.filename)
            return

        audio_data = audio_file.read(self.payload_length)

        while self._running:
            try:
                header = rtp_header(2, 0, 0, 0, self.payload_type, seq, ts, self.ssrc)
                packet = (time_play, ts, seq, self.ssrc, header + audio_data)
                self.data_queue.put(packet, True, 1)

                audio_data = audio_file.read(self.payload_length)
                if len(audio_data) != self.payload_length:
                    audio_file.seek(0)
                    audio_data = audio_file.read(self.payload_length)

                seq += 1
                ts += self.timestamp_delta
                time_play += self.ptime_ms

            except queue.Full:
                time.sleep(self.ptime_ms * 0.001)

        audio_file.close()
        logger.debug("AudioFileStreamer stopped")


class DTMFStreamer:
    """Generates RFC 2833 DTMF telephone events."""

    DTMF_MAP = {
        "0": 0, "1": 1, "2": 2, "3": 3, "4": 4,
        "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
        "*": 10, "#": 11, "A": 12, "B": 13, "C": 14, "D": 15,
    }

    def __init__(self, event: str, volume: int = 10, duration_ms: int = 160,
                 payload_type: int = 101):
        self.event_code = self.DTMF_MAP.get(str(event), int(event))
        self.volume = volume
        self.duration_samples = duration_ms * 8  # at 8kHz
        self.payload_type = payload_type
        self.first_timestamp = 0
        self.progress = 0

    def get_replacement_packet(self, timestamp, seq, ssrc) -> Optional[bytes]:
        if not self.first_timestamp:
            self.first_timestamp = timestamp

        self.progress = timestamp - self.first_timestamp
        if self.progress > self.duration_samples:
            return None

        end_flag = 1 if self.progress >= self.duration_samples else 0
        event_data = struct.pack(">BBH",
                                  self.event_code,
                                  ((end_flag & 0x1) << 7) | (self.volume & 0x3F),
                                  self.progress & 0xFFFF)

        header = rtp_header(2, 0, 0, 1 if end_flag else 0,
                           self.payload_type, seq, self.first_timestamp, ssrc)
        return header + event_data


class ToneGenerator:
    """Generates a sine wave tone for in-band signaling."""

    def __init__(self, frequency: float = 440.0, volume: float = 0.8,
                 sample_rate: int = 8000, payload_type: int = 8):
        self.frequency = frequency
        self.volume = volume
        self.sample_rate = sample_rate
        self.payload_type = payload_type
        self.phase = 0.0

    def generate_packet(self, num_samples: int = 160) -> bytes:
        """Generate a packet of tone samples (a-law encoded)."""
        samples = bytearray(num_samples)
        for i in range(num_samples):
            t = (self.phase + i) / self.sample_rate
            value = math.sin(2 * math.pi * self.frequency * t) * self.volume * 127
            # Simple linear to a-law approximation
            samples[i] = max(0, min(255, int(value + 128)))
        self.phase += num_samples
        return bytes(samples)


class CaptureFileStreamer(BaseStreamer):
    """Replays RTP packets from a pcap capture file."""

    def __init__(self, filename: str, ssrc: Optional[int] = None,
                 payload_type: Optional[int] = None):
        super().__init__("CaptureFileStreamer")
        self.filename = filename
        self.ssrc = ssrc
        self.payload_type = payload_type
        self._seq = 0
        self._start_time = None

    def _parse_rtp_from_udp(self, udp_data: bytes):
        """Extract RTP fields from UDP payload."""
        if len(udp_data) < 12:
            return None
        head, seq, ts, ssrc = struct.unpack(">HHLL", udp_data[:12])
        version = head >> 14
        pt = head & 0x7F
        if version != 2:
            return None
        return {"version": version, "payload_type": pt, "seq": seq,
                "timestamp": ts, "ssrc": ssrc, "data": udp_data}

    def run(self):
        try:
            import dpkt
        except ImportError:
            logger.error("dpkt library required for pcap replay. Install with: pip install dpkt")
            return

        try:
            capture_file = self._open_media_file(self.filename)
        except FileNotFoundError:
            logger.error("Capture file not found: %s", self.filename)
            return

        pcap = dpkt.pcap.Reader(capture_file)

        for pkt_time, pkt_buf in pcap:
            if not self._running:
                break

            try:
                eth = dpkt.ethernet.Ethernet(pkt_buf)
                if not isinstance(eth.data, dpkt.ip.IP):
                    continue
                ip = eth.data
                if not isinstance(ip.data, dpkt.udp.UDP):
                    continue
                udp = ip.data
            except Exception:
                continue

            rtp = self._parse_rtp_from_udp(udp.data)
            if rtp is None:
                continue

            # Filter by SSRC
            if self.ssrc is None:
                self.ssrc = rtp["ssrc"]
            if rtp["ssrc"] != self.ssrc:
                continue

            # Filter by payload type
            if self.payload_type is None:
                self.payload_type = rtp["payload_type"]
            if rtp["payload_type"] != self.payload_type:
                continue

            if self._start_time is None:
                self._start_time = pkt_time

            time_play_ms = (pkt_time - self._start_time) * 1000.0
            packet = (time_play_ms, rtp["timestamp"], self._seq, self.ssrc, udp.data)
            self._seq += 1

            while self._running:
                try:
                    self.data_queue.put(packet, True, 1)
                    break
                except queue.Full:
                    time.sleep(0.001)

        capture_file.close()


class RTPSession(threading.Thread):
    """
    Manages a single RTP stream - sends and receives RTP packets.
    Connects a content streamer to a UDP socket.
    """

    def __init__(self, local_ip: str, local_port: int,
                 remote_ip: str, remote_port: int,
                 existing_socket: socket.socket = None):
        super().__init__(daemon=True, name=f"RTP-{local_port}")
        self.local_ip = local_ip
        self.local_port = local_port
        self.remote_ip = remote_ip
        self.remote_port = remote_port
        self._running = True
        self._dtmf_streamer: Optional[DTMFStreamer] = None
        self._manage_socket = existing_socket is None

        if existing_socket:
            self.sock = existing_socket
            self.local_ip = self.sock.getsockname()[0]
            self.local_port = self.sock.getsockname()[1]
        else:
            family = socket.AF_INET6 if is_ipv6(local_ip) else socket.AF_INET
            self.sock = socket.socket(family, socket.SOCK_DGRAM)
            self.sock.bind((local_ip, local_port))

        # Stats
        self.packets_sent = 0
        self.packets_received = 0
        self.bytes_sent = 0

    def send_dtmf(self, digit: str, volume: int = 10, duration_ms: int = 160,
                  payload_type: int = 101):
        """Send a DTMF digit via RFC 2833."""
        self._dtmf_streamer = DTMFStreamer(digit, volume, duration_ms, payload_type)

    def stream(self, streamer: BaseStreamer):
        """Stream RTP from the given content streamer."""
        try:
            streamer.start()
        except Exception:
            logger.exception("Failed to start RTP content streamer")
            return

        start_time = time.time()
        first_packet = True
        time_play_zero = 0

        try:
            while self._running:
                try:
                    time_play, ts, seq, ssrc, data = streamer.get_next_packet()
                    if first_packet:
                        time_play_zero = time_play
                        first_packet = False
                    time_play -= time_play_zero
                except queue.Empty:
                    logger.debug("No RTP data available")
                    continue

                # Handle DTMF replacement
                if self._dtmf_streamer:
                    replacement = self._dtmf_streamer.get_replacement_packet(ts, seq, ssrc)
                    if replacement:
                        data = replacement
                    else:
                        self._dtmf_streamer = None

                # Timing control
                wait = start_time - time.time() + time_play * 0.001
                if wait > 0:
                    time.sleep(wait)

                self.sock.sendto(data, (self.remote_ip, self.remote_port))
                self.packets_sent += 1
                self.bytes_sent += len(data)

                # Drain incoming packets (non-blocking)
                while True:
                    try:
                        ready, _, _ = select.select([self.sock], [], [], 0)
                        if self.sock in ready:
                            self.sock.recvfrom(RECV_BUFFER)
                            self.packets_received += 1
                        else:
                            break
                    except Exception:
                        break

        except Exception as e:
            if self._running:
                logger.exception("RTP streaming error: %s", e)

        streamer.stop()
        self._close()

    def _close(self):
        try:
            if self._manage_socket:
                self.sock.close()
                logger.debug("RTP socket closed on port %d", self.local_port)
        except Exception:
            pass

    def stop(self):
        self._running = False

    def run(self):
        """Default run method - override or call stream() directly."""
        pass

    def to_dict(self):
        return {
            "local": f"{self.local_ip}:{self.local_port}",
            "remote": f"{self.remote_ip}:{self.remote_port}",
            "packets_sent": self.packets_sent,
            "packets_received": self.packets_received,
            "bytes_sent": self.bytes_sent,
            "running": self._running,
        }


class RTPAudioSession(RTPSession):
    """RTP session that streams an audio file."""

    def __init__(self, local_ip, local_port, remote_ip, remote_port,
                 audio_file="test.g711a", ptime=20, **kwargs):
        super().__init__(local_ip, local_port, remote_ip, remote_port, **kwargs)
        self.audio_file = audio_file
        self.ptime = ptime

    def run(self):
        afs = AudioFileStreamer(self.audio_file, 0, random.randint(0, 0xFFFFFFFF), self.ptime)
        self.stream(afs)


class RTPCaptureSession(RTPSession):
    """RTP session that replays a pcap capture."""

    def __init__(self, local_ip, local_port, remote_ip, remote_port,
                 capture_file: str, ssrc=None, payload_type=None, **kwargs):
        super().__init__(local_ip, local_port, remote_ip, remote_port, **kwargs)
        self.capture_file = capture_file
        self.ssrc = ssrc
        self.payload_type = payload_type

    def run(self):
        cfs = CaptureFileStreamer(self.capture_file, self.ssrc, self.payload_type)
        self.stream(cfs)


class RTPPortManager:
    """Manages allocation of RTP ports from a configured range."""

    def __init__(self, config: Config = None):
        config = config or Config()
        self.min_port = config.min_rtp_port
        self.max_port = config.max_rtp_port
        self._allocated: set[int] = set()
        self._lock = threading.Lock()

    def allocate(self) -> int:
        """Allocate an even-numbered RTP port (RTP uses even, RTCP uses odd)."""
        with self._lock:
            for port in range(self.min_port, self.max_port, 2):
                if port not in self._allocated:
                    self._allocated.add(port)
                    return port
        raise RuntimeError("No RTP ports available")

    def release(self, port: int):
        with self._lock:
            self._allocated.discard(port)

    @property
    def available(self) -> int:
        return (self.max_port - self.min_port) // 2 - len(self._allocated)
