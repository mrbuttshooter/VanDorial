"""
Generate g711a.raw — the headerless G.711 A-law sample file SIPp's rtp_stream
plays as the loop's media (design: optional RTP media).

rtp_stream needs RAW codec samples (no pcap/wav header) — unlike play_pcap_audio
(which needs a structured pcap and a raw socket). This is just bytes, so there's
none of the pcap-format fragility. One second of a 400 Hz tone at 8 kHz = 8000
A-law bytes (payload type 8). rtp_stream loops it for the whole call, so the
switch sees continuous real media energy (not digital silence, which some routes
suppress). Reproducible (pure math); re-running yields a byte-identical file.

    python3 gencall/scenarios/media/make_g711a_raw.py
"""

import math
import os

RATE = 8000      # 8 kHz
FREQ = 400       # Hz tone
AMPL = 2000      # within the 13-bit A-law magnitude range (max 4095)
SECONDS = 1      # rtp_stream loops it for the call


def linear_to_alaw(sample: int) -> int:
    """ITU-T G.711 A-law encode of a signed (13-bit-range) linear sample."""
    sign = ((~sample) >> 8) & 0x80
    if not sign:
        sample = -sample
    if sample > 0xFFF:
        sample = 0xFFF
    if sample >= 256:
        exponent = 7
        mask = 0x4000
        while (sample & mask) == 0 and exponent > 0:
            exponent -= 1
            mask >>= 1
        mantissa = (sample >> (exponent + 3)) & 0x0F
        alaw = (exponent << 4) | mantissa
    else:
        alaw = sample >> 4
    return (alaw ^ sign ^ 0x55) & 0xFF


def build() -> bytes:
    out = bytearray()
    for n in range(RATE * SECONDS):
        s = int(AMPL * math.sin(2 * math.pi * FREQ * n / RATE))
        out.append(linear_to_alaw(s))
    return bytes(out)


def main() -> None:
    data = build()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "g711a.raw")
    with open(path, "wb") as fh:
        fh.write(data)
    print(f"wrote {len(data)} A-law bytes -> {path}")


if __name__ == "__main__":
    main()
