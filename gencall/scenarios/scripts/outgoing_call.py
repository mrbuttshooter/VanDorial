"""
GenCall - Outgoing Call Engine (UAC)

Intelligent outgoing call generator with:
  - Real traffic shaping that actually works (time-based call probability)
  - Smart codec negotiation from SDP
  - Peer liveness monitoring during calls
  - CSV number pool with efficient random access
  - Proper call lifecycle: INVITE -> handle responses -> RTP -> BYE
  - Guaranteed resource cleanup

Replaces the old Sigma outgoing script which had:
  - 24 identical if/else blocks that never actually blocked a call
  - 6 copy-pasted duration blocks
  - CSV reading by iterating row-by-row
  - Overlapping codec regex that could fire multiple times
  - No resource cleanup guarantees
"""

import datetime
import random
import re
import csv
import time
import os
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("gencall.scenario.outgoing_call")


# ═══════════════════════════════════════════════════════════════════════════════
#  TRAFFIC PROFILE - Controls WHEN and HOW LONG calls happen
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TrafficWindow:
    """Defines call behavior for a time window."""
    start_hour: int
    end_hour: int
    call_probability: float      # 0.0 to 1.0 - chance a call is placed
    min_duration_sec: int        # min call duration if connected
    max_duration_sec: int        # max call duration if connected
    description: str = ""


# Realistic office/call center traffic pattern
# Customize these to match your traffic model
TRAFFIC_PROFILE = [
    TrafficWindow(0,  6,  0.05, 60,   180,  "Night - minimal traffic"),
    TrafficWindow(6,  8,  0.30, 120,  300,  "Early morning - ramping up"),
    TrafficWindow(8,  12, 0.85, 300,  600,  "Morning peak - heavy traffic"),
    TrafficWindow(12, 13, 0.50, 180,  400,  "Lunch dip"),
    TrafficWindow(13, 17, 0.90, 450,  700,  "Afternoon peak - heaviest"),
    TrafficWindow(17, 19, 0.60, 300,  550,  "Evening wind-down"),
    TrafficWindow(19, 22, 0.35, 200,  500,  "Evening - moderate"),
    TrafficWindow(22, 24, 0.10, 120,  300,  "Late night - low traffic"),
]


def get_traffic_window(hour: int) -> TrafficWindow:
    """Get the traffic parameters for the current hour."""
    for window in TRAFFIC_PROFILE:
        if window.start_hour <= hour < window.end_hour:
            return window
    # Fallback
    return TrafficWindow(0, 24, 0.5, 120, 300, "Default")


def should_place_call(hour: int) -> bool:
    """
    Probabilistic call gating based on time of day.
    Returns True if a call should be placed right now.
    """
    window = get_traffic_window(hour)
    roll = random.random()
    should_call = roll < window.call_probability
    logger.info("Traffic gate: hour=%d, probability=%.0f%%, roll=%.2f -> %s",
                hour, window.call_probability * 100, roll,
                "CALL" if should_call else "SKIP")
    return should_call


# ═══════════════════════════════════════════════════════════════════════════════
#  NUMBER POOL - Efficient random number selection from CSV
# ═══════════════════════════════════════════════════════════════════════════════

class NumberPool:
    """
    Loads a CSV of phone numbers into memory for fast random access.
    No more iterating 500K rows to pick one number.
    """

    def __init__(self, csv_path: str, delimiter: str = ";"):
        self.callers: list[str] = []
        self.callees: list[str] = []
        self._load(csv_path, delimiter)

    def _load(self, path: str, delimiter: str):
        if not os.path.exists(path):
            raise FileNotFoundError(f"Number pool CSV not found: {path}")

        with open(path, "r") as f:
            reader = csv.reader(f, delimiter=delimiter)
            for row in reader:
                if len(row) >= 2:
                    if row[0].strip():
                        self.callers.append(row[0].strip())
                    if row[1].strip():
                        self.callees.append(row[1].strip())

        if not self.callers or not self.callees:
            raise ValueError(f"Number pool is empty: {path}")

        logger.info("Number pool loaded: %d callers, %d callees", len(self.callers), len(self.callees))

    def random_caller(self) -> str:
        return random.choice(self.callers)

    def random_callee(self) -> str:
        return random.choice(self.callees)

    def random_pair(self) -> tuple[str, str]:
        return self.random_caller(), self.random_callee()


# ═══════════════════════════════════════════════════════════════════════════════
#  CODEC NEGOTIATION - Parse SDP and pick the right audio file
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Codec:
    payload_type: int
    name: str
    file_extension: str
    priority: int  # lower = preferred


# Codec priority order - first match wins
CODEC_TABLE = [
    Codec(8,  "PCMA",  "g711a", 1),   # G.711 A-law
    Codec(0,  "PCMU",  "g711u", 2),   # G.711 u-law
    Codec(18, "G729",  "g729a", 3),   # G.729
]


def negotiate_codec(sdp_data: str) -> Optional[Codec]:
    """
    Parse the SDP m=audio line and return the best matching codec.
    Checks preferred codec first, then falls back through the list.
    """
    # Extract payload types from m=audio line
    m_match = re.search(r"m=audio\s+\d+\s+RTP/AVP\s+([\d\s]+)", sdp_data)
    if not m_match:
        logger.warning("No m=audio line found in SDP")
        return None

    offered_pts = [int(pt) for pt in m_match.group(1).split()]
    logger.debug("SDP offered payload types: %s", offered_pts)

    # First pass: check if our preferred codecs are the primary offer
    for codec in CODEC_TABLE:
        if offered_pts and offered_pts[0] == codec.payload_type:
            logger.info("Codec negotiated: %s (primary offer)", codec.name)
            return codec

    # Second pass: check if any of our codecs are in the offer at all
    for codec in CODEC_TABLE:
        if codec.payload_type in offered_pts:
            logger.info("Codec negotiated: %s (fallback)", codec.name)
            return codec

    logger.warning("No supported codec found in SDP offer: %s", offered_pts)
    return None


def pick_audio_file(codec: Codec, audio_dir: str = "/opt/gencall/media") -> str:
    """Pick a random audio file matching the negotiated codec."""
    # Look for files like audio1.g711a, audio2.g711a, leg2.g711a, etc.
    candidates = []
    if os.path.isdir(audio_dir):
        for f in os.listdir(audio_dir):
            if f.endswith(f".{codec.file_extension}"):
                candidates.append(f)

    if candidates:
        chosen = random.choice(candidates)
        logger.debug("Audio file selected: %s (from %d candidates)", chosen, len(candidates))
        return chosen

    # Fallback to a default name
    fallback = f"audio1.{codec.file_extension}"
    logger.debug("No audio files found in %s, using fallback: %s", audio_dir, fallback)
    return fallback


# ═══════════════════════════════════════════════════════════════════════════════
#  PEER MONITORING - Check if the remote side is still alive during a call
# ═══════════════════════════════════════════════════════════════════════════════

def monitor_call(ctx, dialog, call_key: str, duration_sec: int,
                 check_interval: int = 3) -> Optional[object]:
    """
    Hold the call for the specified duration while monitoring peer liveness.

    Periodically checks shared state to verify the remote (incoming) scenario
    is still alive. Returns early if:
      - Remote peer sends a BYE
      - Remote peer is detected as disconnected
      - A mid-call message is received (OPTIONS, re-INVITE, etc.)

    Args:
        ctx: Scenario context
        dialog: Active SIP dialog
        call_key: Shared variable key for this call
        duration_sec: How long to hold the call
        check_interval: Seconds between liveness checks

    Returns:
        The SIP message that ended the wait, or None on timeout/disconnect.
    """
    reverse_key = call_key + "_reverse"
    num_checks = duration_sec // check_interval
    remaining = duration_sec - (num_checks * check_interval)

    logger.info("Call monitor: duration=%ds, %d checks every %ds, %ds remaining",
                duration_sec, num_checks, check_interval, remaining)

    ctx.set_value(reverse_key, True)

    for check_num in range(1, num_checks + 1):
        # Reset our flag so the remote side must set it back
        ctx.set_value(call_key, False, ttl=600)
        ctx.set_value(reverse_key, True, ttl=600)

        # Wait for a message or timeout
        message = dialog.wait_message(check_interval)

        if message:
            # Got a SIP message during the call
            return message

        # Check if remote side is still alive
        if not ctx.get_value(call_key):
            logger.warning("Peer disconnected (check %d/%d)", check_num, num_checks)
            return None

    # Final wait for remaining time
    if remaining > 0:
        message = dialog.wait_message(remaining)
        if message:
            return message

    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  CALL FLOW HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

def handle_cancel(ctx, dialog, messages, parameters, invite, start_time, reason: str):
    """Send CANCEL and handle the response sequence."""
    cancel = messages.CANCEL(parameters)
    logger.info("Sending CANCEL: %s", reason)
    dialog.send(cancel)

    reply = cancel.get_reply(5)
    if reply and reply.get_code() == "200":
        logger.debug("200 OK on CANCEL received")
        # Wait for 487 Request Terminated
        final = cancel.get_reply(10)
        if final:
            ack = messages.ACK(parameters)
            final.reply(ack)

    ctx.set_availability_undetermined(time.time() - start_time)
    ctx.quit("Call Canceled", reason)


def handle_error_response(ctx, dialog, messages, parameters, response,
                          start_time, calling, called):
    """Handle non-200 final response to INVITE."""
    if response:
        code = response.get_code()
        ack = messages.ACK_NON200OK(parameters)
        response.reply(ack)
        logger.warning("Call failed with %s", code)
        ctx.set_availability_undetermined(time.time() - start_time)
        ctx.quit("Call failed", f"Error {code} | {calling} -> {called}")
    else:
        # No response at all - send CANCEL
        handle_cancel(ctx, dialog, messages, parameters, None, start_time,
                      "No final response received")


def handle_bye(ctx, dialog, messages, parameters, message, rtp_stream,
               start_time, calling, called, picked_up: bool):
    """Process an incoming BYE and clean up."""
    bye_ok = messages.BYE_200(parameters)
    message.reply(bye_ok)

    if rtp_stream:
        rtp_stream.stop()
        logger.info("RTP streaming ended")

    elapsed = time.time() - start_time

    if picked_up:
        ctx.set_availability_success(elapsed)
        ctx.quit("Call ended", f"Normal release | {calling} -> {called}")
    else:
        ctx.set_availability_undetermined(elapsed)
        ctx.quit("Call ended", f"BYE received | {calling} -> {called}")


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN SCENARIO
# ═══════════════════════════════════════════════════════════════════════════════

def run(ctx):
    """
    Outgoing call scenario entry point.

    Full flow:
        1. Traffic gating (time-based probability)
        2. Pick random caller/callee from number pool
        3. INVITE with dynamic SDP
        4. Handle provisional responses (100/180/183)
        5. Negotiate codec from 200 OK SDP
        6. Start RTP streaming
        7. Monitor call with peer liveness checks
        8. BYE and cleanup
    """
    messages = ctx.messages
    parameters = ctx.parameters

    # ── 1. Traffic shaping ─────────────────────────────────────────────────
    hour = datetime.datetime.now().hour
    jitter = random.uniform(0.5, 5.0)
    ctx.sleep(jitter)

    if not should_place_call(hour):
        logger.info("Call skipped by traffic shaper")
        ctx.quit("Skipped", "Traffic shaper")
        return

    # ── 2. Pick numbers ────────────────────────────────────────────────────
    pool = NumberPool(ctx.config.get("number_pool_csv",
                                      "/opt/gencall/media/numbers.csv"))
    calling, called = pool.random_pair()
    parameters["fromNumber"] = calling
    parameters["toNumber"] = called
    logger.info("Call: %s -> %s", calling, called)

    # ── 3. Set up shared state ─────────────────────────────────────────────
    dialog = ctx.new_dialog()
    call_key = f"{calling}{called}"
    ctx.set_value(call_key, False, ttl=600)
    start_time = time.time()
    rtp_stream = None

    try:
        # ── 4. Send INVITE ─────────────────────────────────────────────────
        invite = messages.INVITE_DYNAMIC_RTP_G729(parameters)
        raw_invite = dialog.send(invite)

        # ── 5. Wait for provisional response ───────────────────────────────
        provisional = invite.ignore_replies("100", timeout=15)

        if not provisional:
            handle_cancel(ctx, dialog, messages, parameters, invite, start_time,
                          "No provisional response within 15s")
            return

        code = provisional.get_code()

        # ── 6. Handle ringing ──────────────────────────────────────────────
        if code in ("180", "183", "181"):
            logger.info("Ringing (%s)", code)

            # Random early cancel (1 in 20 chance for realism)
            if random.random() < 0.05:
                ring_wait = random.uniform(1.0, 3.0)
                logger.info("Simulating caller hang-up after %.1fs", ring_wait)
                early = invite.ignore_replies("180", "183", timeout=ring_wait)
                if not early:
                    handle_cancel(ctx, dialog, messages, parameters, invite,
                                  start_time, "Caller abandoned (simulated)")
                    return
                provisional = early
            else:
                provisional = invite.ignore_replies("180", "183", timeout=15)

        # ── 7. Check final response ───────────────────────────────────────
        if not provisional or provisional.get_code() != "200":
            handle_error_response(ctx, dialog, messages, parameters,
                                  provisional, start_time, calling, called)
            return

        answer = provisional  # This is our 200 OK
        logger.info("Call answered (200 OK)")

        # ── 8. Codec negotiation + RTP ────────────────────────────────────
        if raw_invite.get_rtp_port() and answer.get_rtp_port():
            codec = negotiate_codec(answer.data)
            if codec:
                audio_file = pick_audio_file(codec)
                rtp_stream = ctx.rtp_streamer(raw_invite, answer, audio_file)
                rtp_stream.start()
                logger.info("RTP started: codec=%s, file=%s", codec.name, audio_file)
            else:
                logger.warning("No codec match - call continues without RTP")

        # ── 9. Send ACK ──────────────────────────────────────────────────
        ack = messages.ACK(parameters)
        answer.reply(ack)

        # ── 10. Check if remote picked up (Dory check) ───────────────────
        picked_up = ctx.get_value(call_key)

        if not picked_up:
            logger.warning("Call not picked up by remote handler - releasing in 0.1s")
            ctx.sleep(0.1)
            _send_bye_and_quit(ctx, dialog, messages, parameters, rtp_stream,
                               start_time, calling, called, success=False)
            return

        logger.info("Call confirmed by remote handler")
        ctx.set_value(call_key, False, ttl=600)

        # ── 11. Hold the call (with peer monitoring) ──────────────────────
        window = get_traffic_window(hour)
        duration = random.randint(window.min_duration_sec, window.max_duration_sec)
        logger.info("Call duration: %ds (window: %s)", duration, window.description)

        message = monitor_call(ctx, dialog, call_key, duration)

        # ── 12. Handle whatever ended the call ────────────────────────────
        if message:
            msg_code = message.get_code()

            if msg_code == "BYE":
                handle_bye(ctx, dialog, messages, parameters, message,
                           rtp_stream, start_time, calling, called, picked_up=True)
                return

            elif msg_code == "OPTIONS":
                options_ok = messages.OPTIONS_200(parameters)
                message.reply(options_ok)
                logger.debug("OPTIONS keep-alive answered mid-call")

            else:
                logger.debug("Unexpected mid-call message: %s", msg_code)

        # ── 13. Normal call end - send BYE ────────────────────────────────
        _send_bye_and_quit(ctx, dialog, messages, parameters, rtp_stream,
                           start_time, calling, called, success=True)

    finally:
        # ── GUARANTEED CLEANUP ────────────────────────────────────────────
        if rtp_stream:
            try:
                rtp_stream.stop()
            except Exception:
                pass


def _send_bye_and_quit(ctx, dialog, messages, parameters, rtp_stream,
                       start_time, calling, called, success: bool):
    """Send BYE, stop RTP, report stats, and quit."""
    bye = messages.BYE(parameters)
    dialog.send(bye)

    if rtp_stream:
        rtp_stream.stop()
        logger.info("RTP streaming ended")

    bye_answer = bye.get_reply(20)
    elapsed = time.time() - start_time

    if bye_answer and bye_answer.get_code() == "200":
        logger.info("Call ended normally after %.1fs", elapsed)

    if success:
        ctx.set_availability_success(elapsed)
        ctx.quit("Call ended", f"Normal | {calling} -> {called} | {elapsed:.1f}s")
    else:
        ctx.set_availability_global_failure(elapsed)
        ctx.quit("Call failed", f"Not picked up by remote | {calling} -> {called}")
