"""
GenCall - Incoming Call Handler (UAS)

Handles inbound SIP calls with full call flow:
  INVITE -> 100 -> 180 -> 200 OK (with RTP) -> ACK
  Handles mid-call: re-INVITE, OPTIONS keep-alive
  BYE -> 200 OK (cleanup)

Original Sigma scenario rewritten for GenCall with:
  - Proper SIP URI parsing
  - Robust error handling
  - Guaranteed RTP cleanup
  - Clean state machine for mid-call events
  - Configurable ring time and audio file
"""

import random
import re
import logging

logger = logging.getLogger("gencall.scenario.incoming_call")


# ─── SIP URI Helpers ───────────────────────────────────────────────────────────

def parse_sip_user(header_value: str) -> str:
    """
    Extract the user part from a SIP From/To header.

    Examples:
        '"John" <sip:john@10.0.0.1:5060>;tag=abc' -> 'john'
        '<sip:+15551234567@proxy.com>'             -> '+15551234567'
        'sip:user@host'                            -> 'user'
    """
    match = re.search(r"sip:([^@]+)@", header_value)
    if match:
        return match.group(1)
    return header_value


def parse_sip_domain(header_value: str) -> str:
    """Extract domain/host from a SIP URI."""
    match = re.search(r"sip:[^@]+@([^>;:\s]+)", header_value)
    if match:
        return match.group(1)
    return ""


# ─── Scenario Entry Point ─────────────────────────────────────────────────────

def run(ctx):
    """
    Main scenario handler. Called by GenCall engine with a scenario context.

    Args:
        ctx: ScenarioContext providing:
            - ctx.wait_new_session(method) -> incoming request
            - ctx.messages                 -> SIP message builder
            - ctx.parameters               -> connector parameters
            - ctx.set_value(key, val)      -> shared variable store
            - ctx.rtp_streamer(...)        -> RTP stream factory
            - ctx.log(msg)                 -> scenario logger
            - ctx.sleep(seconds)           -> non-blocking sleep
            - ctx.quit(reason, detail)     -> end scenario
    """

    # ── Configuration ──────────────────────────────────────────────────────
    RING_TIME_MIN = 1       # min seconds before answering
    RING_TIME_MAX = 10      # max seconds before answering
    AUDIO_FILE = "words2.g711u"
    CONNECTOR_LEVEL = False

    messages = ctx.messages
    parameters = ctx.parameters
    rtp_stream = None

    # ── Wait for incoming INVITE ───────────────────────────────────────────
    invite = ctx.wait_new_session("INVITE")
    dialog = invite.context_dialog

    # ── Send 100 Trying (stop retransmissions) ─────────────────────────────
    trying = messages.INVITE_100(parameters)
    invite.reply(trying)

    # ── Parse caller and callee identities ─────────────────────────────────
    from_header = dialog.last_from
    to_header = dialog.last_to

    caller = parse_sip_user(from_header)
    callee = parse_sip_user(to_header)
    call_key = f"{caller}{callee}"

    logger.info("Incoming call: %s -> %s", caller, callee)

    # ── Send 180 Ringing ───────────────────────────────────────────────────
    ringing = messages.INVITE_180(parameters)
    invite.reply(ringing)

    # ── Simulate ring time ─────────────────────────────────────────────────
    ring_time = random.randint(RING_TIME_MIN, RING_TIME_MAX)
    logger.debug("Ringing for %d seconds", ring_time)
    ctx.sleep(ring_time)

    # ── Answer with 200 OK + SDP ───────────────────────────────────────────
    ctx.set_value(call_key, "True", CONNECTOR_LEVEL)

    ok_response = messages.INVITE_200_Dory(parameters)
    raw_ok = invite.reply(ok_response)

    # ── Start RTP media stream ─────────────────────────────────────────────
    try:
        if raw_ok.get_rtp_port() and invite.get_rtp_port():
            rtp_stream = ctx.rtp_streamer(raw_ok, invite, AUDIO_FILE)
            rtp_stream.start()
            logger.info("RTP streaming started on port %s -> %s",
                        raw_ok.get_rtp_port(), invite.get_rtp_port())
        else:
            logger.warning("RTP ports not available - proceeding without media")
    except Exception as e:
        logger.error("Failed to start RTP: %s", e)

    # ── Wait for ACK ──────────────────────────────────────────────────────
    ack = ok_response.get_reply()
    if ack and ack.get_code() == "ACK":
        logger.info("Call established (ACKed)")
    else:
        logger.warning("Expected ACK, got: %s", ack.get_code() if ack else "timeout")

    # ── Mid-call event loop ────────────────────────────────────────────────
    #    Handle re-INVITEs (codec changes, hold/resume) and OPTIONS pings
    #    until we receive a BYE.
    try:
        _handle_mid_call(ctx, dialog, messages, parameters, raw_ok)
    finally:
        # ── Guaranteed RTP cleanup ─────────────────────────────────────────
        if rtp_stream:
            rtp_stream.stop()
            logger.info("RTP streaming ended")

    ctx.quit("call ended", "released by remote peer")


# ─── Mid-Call Event Loop ───────────────────────────────────────────────────────

def _handle_mid_call(ctx, dialog, messages, parameters, raw_ok):
    """
    Process mid-call SIP messages until BYE is received.

    Handles:
        - OPTIONS: reply 200 OK (keep-alive)
        - re-INVITE: reply 200 OK with updated SDP, wait for ACK
        - BYE: reply 200 OK and return
    """

    while True:
        message = dialog.wait_message()

        if message is None:
            logger.warning("Dialog timeout - no message received")
            break

        code = message.get_code()
        logger.debug("Mid-call message: %s", code)

        if code == "BYE":
            ok_bye = messages.BYE_200(parameters)
            message.reply(ok_bye)
            logger.info("BYE received - call ended")
            return

        elif code == "OPTIONS":
            options_ok = messages.OPTIONS_200(parameters)
            message.reply(options_ok)
            logger.debug("OPTIONS keep-alive answered")

        elif code == "INVITE":
            # re-INVITE (hold, resume, codec change)
            _handle_reinvite(dialog, messages, parameters, message, raw_ok)

        else:
            logger.debug("Ignoring unexpected mid-call message: %s", code)


def _handle_reinvite(dialog, messages, parameters, reinvite, raw_ok):
    """
    Handle a mid-call re-INVITE (hold/resume/codec renegotiation).

    Flow: re-INVITE -> 200 OK -> ACK
    """
    try:
        # Preserve the local RTP port from the original answer
        parameters["localRTPPort"] = raw_ok.get_rtp_port()

        ok_reinvite = messages.INVITE_200(parameters)
        reinvite.reply(ok_reinvite)

        # Wait for ACK on the re-INVITE
        ack = dialog.wait_message()
        if ack and ack.get_code() == "ACK":
            logger.info("re-INVITE handled successfully")
        else:
            logger.warning("Expected ACK for re-INVITE, got: %s",
                           ack.get_code() if ack else "timeout")

    except Exception as e:
        logger.error("re-INVITE handling failed: %s", e)
