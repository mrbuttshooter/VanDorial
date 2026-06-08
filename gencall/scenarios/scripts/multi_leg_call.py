"""
GenCall - Multi-Leg Call Scenario

Simulates complex call flows involving multiple legs:
  - Call transfer (blind and attended)
  - Conference bridges (3+ parties)
  - Call forwarding chains
  - Sequential forking
  - IVR navigation with DTMF

These scenarios test PBX/switch behavior under real-world conditions.
"""

import time
import random
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("gencall.scenario.multi_leg")


@dataclass
class CallLeg:
    """Represents one leg of a multi-party call."""
    leg_id: str
    caller: str
    callee: str
    dialog: object = None
    rtp_stream: object = None
    state: str = "idle"  # idle, ringing, connected, transferred, ended
    start_time: float = 0.0
    connect_time: float = 0.0
    end_time: float = 0.0

    @property
    def duration(self) -> float:
        if self.connect_time and self.end_time:
            return self.end_time - self.connect_time
        return 0.0

    def to_dict(self) -> dict:
        return {
            "leg_id": self.leg_id,
            "caller": self.caller,
            "callee": self.callee,
            "state": self.state,
            "duration": round(self.duration, 1),
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  BLIND TRANSFER
# ═══════════════════════════════════════════════════════════════════════════════

def run_blind_transfer(ctx):
    """
    Blind (unattended) transfer scenario:
      1. A calls B (INVITE, 200 OK, ACK)
      2. A sends REFER to B (transfer to C)
      3. B calls C (new INVITE)
      4. A drops out, B-C call continues
      5. B or C hangs up

    Tests: REFER handling, Subscription-State, NOTIFY
    """
    messages = ctx.messages
    parameters = ctx.parameters

    # Parties
    party_a = parameters.get("party_a", "1001")
    party_b = parameters.get("party_b", "1002")
    party_c = parameters.get("party_c", "1003")

    logger.info("Blind transfer: %s -> %s, transfer to %s", party_a, party_b, party_c)

    # ── Leg 1: A calls B ──────────────────────────────────────────────────
    leg1 = CallLeg(leg_id="A-B", caller=party_a, callee=party_b)
    leg1.start_time = time.time()

    params_ab = dict(parameters)
    params_ab["fromNumber"] = party_a
    params_ab["toNumber"] = party_b

    dialog_ab = ctx.new_dialog()
    invite_ab = messages.INVITE_DYNAMIC_RTP_G729(params_ab)
    raw_invite = dialog_ab.send(invite_ab)

    response = invite_ab.ignore_replies("100", "180", "183", timeout=15)
    if not response or response.get_code() != "200":
        logger.error("Leg A-B failed: %s", response.get_code() if response else "timeout")
        ctx.quit("Transfer failed", "Initial call failed")
        return

    leg1.state = "connected"
    leg1.connect_time = time.time()

    # Start RTP
    rtp = None
    if raw_invite.get_rtp_port() and response.get_rtp_port():
        rtp = ctx.rtp_streamer(raw_invite, response, "audio1.g711a")
        rtp.start()
        leg1.rtp_stream = rtp

    # ACK
    ack = messages.ACK(params_ab)
    response.reply(ack)

    logger.info("Leg A-B connected, holding for 5s before transfer")
    ctx.sleep(5)

    # ── REFER: A transfers B to C ─────────────────────────────────────────
    logger.info("Sending REFER: transfer %s to %s", party_b, party_c)

    params_refer = dict(params_ab)
    params_refer["referTo"] = party_c
    refer = messages.REFER(params_refer)
    dialog_ab.send(refer)

    # Wait for 202 Accepted
    refer_response = refer.get_reply(10)
    if refer_response and refer_response.get_code() == "202":
        logger.info("REFER accepted (202)")
    else:
        logger.warning("REFER response: %s",
                       refer_response.get_code() if refer_response else "timeout")

    # Wait for NOTIFY (subscription state)
    notify = dialog_ab.wait_message(10)
    if notify and notify.get_code() == "NOTIFY":
        ok_notify = messages.NOTIFY_200(params_ab)
        notify.reply(ok_notify)
        logger.info("NOTIFY received - transfer in progress")

    # ── A hangs up leg A-B ────────────────────────────────────────────────
    ctx.sleep(2)
    bye_ab = messages.BYE(params_ab)
    dialog_ab.send(bye_ab)
    bye_ab.get_reply(10)

    if rtp:
        rtp.stop()

    leg1.state = "ended"
    leg1.end_time = time.time()

    logger.info("Blind transfer complete: A-B=%.1fs", leg1.duration)
    ctx.quit("Transfer complete",
             f"Blind transfer {party_a}->{party_b}->{party_c} | A-B={leg1.duration:.1f}s")


# ═══════════════════════════════════════════════════════════════════════════════
#  ATTENDED TRANSFER
# ═══════════════════════════════════════════════════════════════════════════════

def run_attended_transfer(ctx):
    """
    Attended (consultative) transfer:
      1. A calls B (connected)
      2. A puts B on hold (re-INVITE with sendonly)
      3. A calls C (consultation call)
      4. A sends REFER to B with Replaces header (connect B to C)
      5. A drops both legs

    Tests: Hold/resume, REFER with Replaces, dialog replacement
    """
    messages = ctx.messages
    parameters = ctx.parameters

    party_a = parameters.get("party_a", "1001")
    party_b = parameters.get("party_b", "1002")
    party_c = parameters.get("party_c", "1003")

    logger.info("Attended transfer: %s -> %s, consult %s", party_a, party_b, party_c)

    # ── Leg 1: A calls B ──────────────────────────────────────────────────
    params_ab = dict(parameters)
    params_ab["fromNumber"] = party_a
    params_ab["toNumber"] = party_b

    dialog_ab = ctx.new_dialog()
    invite_ab = messages.INVITE_DYNAMIC_RTP_G729(params_ab)
    raw_ab = dialog_ab.send(invite_ab)
    resp_ab = invite_ab.ignore_replies("100", "180", "183", timeout=15)

    if not resp_ab or resp_ab.get_code() != "200":
        ctx.quit("Transfer failed", "Leg A-B setup failed")
        return

    ack_ab = messages.ACK(params_ab)
    resp_ab.reply(ack_ab)
    logger.info("Leg A-B connected")

    ctx.sleep(3)

    # ── Hold B (re-INVITE with sendonly) ──────────────────────────────────
    logger.info("Putting B on hold")
    hold_invite = messages.INVITE_HOLD(params_ab)
    dialog_ab.send(hold_invite)
    hold_resp = hold_invite.ignore_replies("100", timeout=10)
    if hold_resp and hold_resp.get_code() == "200":
        ack_hold = messages.ACK(params_ab)
        hold_resp.reply(ack_hold)
        logger.info("B is on hold")

    # ── Leg 2: A calls C (consultation) ───────────────────────────────────
    params_ac = dict(parameters)
    params_ac["fromNumber"] = party_a
    params_ac["toNumber"] = party_c

    dialog_ac = ctx.new_dialog()
    invite_ac = messages.INVITE_DYNAMIC_RTP_G729(params_ac)
    raw_ac = dialog_ac.send(invite_ac)
    resp_ac = invite_ac.ignore_replies("100", "180", "183", timeout=15)

    if not resp_ac or resp_ac.get_code() != "200":
        # Consultation failed - take B off hold
        logger.warning("Consultation call to C failed, resuming B")
        resume = messages.INVITE_RESUME(params_ab)
        dialog_ab.send(resume)
        resume_resp = resume.get_reply(10)
        if resume_resp:
            resume_resp.reply(messages.ACK(params_ab))
        ctx.quit("Transfer failed", "Consultation call failed")
        return

    ack_ac = messages.ACK(params_ac)
    resp_ac.reply(ack_ac)
    logger.info("Consultation leg A-C connected")

    ctx.sleep(3)

    # ── REFER with Replaces: connect B to C ───────────────────────────────
    logger.info("Sending REFER with Replaces: connecting B to C")
    params_refer = dict(params_ab)
    params_refer["referTo"] = party_c
    params_refer["replaces_call_id"] = dialog_ac  # Dialog to replace
    refer = messages.REFER(params_refer)
    dialog_ab.send(refer)
    refer.get_reply(10)

    # ── Clean up both legs from A's side ──────────────────────────────────
    ctx.sleep(2)

    bye_ab = messages.BYE(params_ab)
    dialog_ab.send(bye_ab)
    bye_ab.get_reply(5)

    bye_ac = messages.BYE(params_ac)
    dialog_ac.send(bye_ac)
    bye_ac.get_reply(5)

    logger.info("Attended transfer complete: B and C now connected")
    ctx.quit("Transfer complete",
             f"Attended transfer {party_a}->{party_b}, consult {party_c}")


# ═══════════════════════════════════════════════════════════════════════════════
#  IVR NAVIGATION
# ═══════════════════════════════════════════════════════════════════════════════

def run_ivr_test(ctx):
    """
    IVR (Interactive Voice Response) navigation test:
      1. Call the IVR number
      2. Wait for answer
      3. Send DTMF sequence (e.g., "1" for English, "3" for billing)
      4. Wait for each menu prompt
      5. Navigate through the menu tree
      6. Hang up

    Config:
        ivr_number:  The IVR number to call
        dtmf_sequence: List of (digit, wait_seconds) tuples
            e.g., [("1", 5), ("3", 3), ("0", 10)]
    """
    messages = ctx.messages
    parameters = ctx.parameters
    config = ctx.config

    ivr_number = config.get("ivr_number", parameters.get("toNumber", "8000"))

    # DTMF sequence: list of (digit, wait_time_after)
    dtmf_sequence = config.get("dtmf_sequence", [
        ("1", 5),   # Press 1 (e.g., language selection)
        ("3", 5),   # Press 3 (e.g., billing)
        ("0", 10),  # Press 0 (e.g., speak to agent)
    ])

    logger.info("IVR test: calling %s, DTMF sequence: %s",
                ivr_number, [d[0] for d in dtmf_sequence])

    params = dict(parameters)
    params["toNumber"] = ivr_number

    dialog = ctx.new_dialog()
    invite = messages.INVITE_DYNAMIC_RTP_G729(params)
    raw_invite = dialog.send(invite)

    response = invite.ignore_replies("100", "180", "183", timeout=15)
    if not response or response.get_code() != "200":
        ctx.quit("IVR test failed", "Call setup failed")
        return

    # Start RTP
    rtp = None
    if raw_invite.get_rtp_port() and response.get_rtp_port():
        rtp = ctx.rtp_streamer(raw_invite, response, "audio1.g711a")
        rtp.start()

    ack = messages.ACK(params)
    response.reply(ack)

    logger.info("IVR connected, starting DTMF navigation")

    # Wait for IVR greeting
    ctx.sleep(3)

    # Navigate the menu
    try:
        for digit, wait_time in dtmf_sequence:
            logger.info("Sending DTMF: %s (waiting %ds after)", digit, wait_time)

            if rtp and hasattr(rtp, 'send_dtmf'):
                rtp.send_dtmf(digit, volume=10, duration_ms=160, payload_type=101)
            else:
                # Fallback: use SIP INFO for DTMF
                info = messages.INFO_DTMF(params, digit=digit)
                dialog.send(info)
                info.get_reply(5)

            # Wait for IVR to process and play next prompt
            message = dialog.wait_message(wait_time)
            if message:
                code = message.get_code()
                if code == "BYE":
                    logger.info("IVR hung up after DTMF %s", digit)
                    bye_ok = messages.BYE_200(params)
                    message.reply(bye_ok)
                    break
                elif code == "OPTIONS":
                    message.reply(messages.OPTIONS_200(params))

        # Hold for final IVR interaction
        logger.info("DTMF sequence complete, holding for 10s")
        final = dialog.wait_message(10)
        if final and final.get_code() == "BYE":
            messages.BYE_200(params)
            final.reply(messages.BYE_200(params))
        else:
            # We hang up
            bye = messages.BYE(params)
            dialog.send(bye)
            bye.get_reply(10)

    finally:
        if rtp:
            rtp.stop()

    logger.info("IVR test complete: navigated %d menu levels", len(dtmf_sequence))
    ctx.quit("IVR test complete", f"Navigated {len(dtmf_sequence)} levels on {ivr_number}")


# ═══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def run(ctx):
    """
    Multi-leg scenario dispatcher.
    Set ctx.config["multi_leg_mode"] to choose:
        "blind_transfer"     - Blind/unattended transfer
        "attended_transfer"  - Attended/consultative transfer
        "ivr"                - IVR DTMF navigation
    """
    mode = ctx.config.get("multi_leg_mode", "blind_transfer")

    dispatch = {
        "blind_transfer": run_blind_transfer,
        "attended_transfer": run_attended_transfer,
        "ivr": run_ivr_test,
    }

    handler = dispatch.get(mode)
    if not handler:
        ctx.quit("Error", f"Unknown multi-leg mode: {mode}")
        return

    handler(ctx)
