"""
Generate the PCMA (G.711 A-law) RTP pcap that SIPp's ``play_pcap_audio`` streams
when a loop has RTP enabled (design: optional media on a loop).

SIPp's pcapplay reads this file, rewrites the IP/UDP headers to the negotiated
media address/port, and replays the RTP payloads at their captured 20 ms spacing.
The codec must match the SDP offer in loop_uac.xml — payload type 8 = PCMA — so
the answering side (UAS, ``-rtp_echo``) echoes a stream it understands.

Run from anywhere; writes ``g711a.pcap`` next to this script:

    python3 gencall/scenarios/media/make_g711a_pcap.py

Reproducible (no timestamps from the wall clock): the capture timeline is built
purely from the 20 ms ptime, so re-running yields a byte-identical file.
"""

import os
import struct

PTIME_MS = 20            # G.711 packetization (50 packets/sec)
SAMPLES = 160            # 8 kHz * 20 ms = 160 samples = 160 bytes PCMA
SECONDS = 10             # ~10 s sample; SIPp loops the call hold over it
ALAW_SILENCE = 0xD5      # A-law encoding of digital silence
PT_PCMA = 8
SSRC = 0x0A0B0C0D


def _ipv4_checksum(header: bytes) -> int:
    s = 0
    for i in range(0, len(header), 2):
        s += (header[i] << 8) + header[i + 1]
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return (~s) & 0xFFFF


def _packet(seq: int, ts: int) -> bytes:
    payload = bytes([ALAW_SILENCE]) * SAMPLES
    # RTP: V=2, P=0, X=0, CC=0, M=0, PT=8.
    rtp = struct.pack("!BBHII", 0x80, PT_PCMA, seq & 0xFFFF, ts & 0xFFFFFFFF, SSRC) + payload

    udp_len = 8 + len(rtp)
    # SIPp rewrites ports/addresses; placeholders are fine. UDP checksum 0 = none.
    udp = struct.pack("!HHHH", 40000, 40000, udp_len, 0) + rtp

    total_len = 20 + udp_len
    ip = struct.pack(
        "!BBHHHBBH4s4s",
        0x45, 0x00, total_len, 0x0000, 0x4000, 64, 17, 0,
        bytes((1, 1, 1, 1)), bytes((2, 2, 2, 2)),
    )
    ip = ip[:10] + struct.pack("!H", _ipv4_checksum(ip)) + ip[12:]

    eth = struct.pack("!6s6sH", b"\x00\x00\x00\x00\x00\x02",
                      b"\x00\x00\x00\x00\x00\x01", 0x0800)
    return eth + ip + udp


def build() -> bytes:
    # pcap global header: magic, ver 2.4, no tz, snaplen, LINKTYPE_ETHERNET (1).
    out = struct.pack("!IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    n = (SECONDS * 1000) // PTIME_MS
    ts_us = 0
    rtp_ts = 0
    for seq in range(n):
        pkt = _packet(seq, rtp_ts)
        sec, usec = divmod(ts_us, 1_000_000)
        out += struct.pack("!IIII", sec, usec, len(pkt), len(pkt)) + pkt
        ts_us += PTIME_MS * 1000
        rtp_ts += SAMPLES
    return out


def main() -> None:
    data = build()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "g711a.pcap")
    with open(path, "wb") as fh:
        fh.write(data)
    print(f"wrote {len(data)} bytes -> {path}  ({(SECONDS*1000)//PTIME_MS} RTP packets)")


if __name__ == "__main__":
    main()
