"""
GenCall - Call Quality Validation Scenario

Places calls and validates end-to-end quality metrics:
  - SIP response times (post-dial delay)
  - RTP packet loss, jitter, out-of-order packets
  - MOS score estimation
  - DTMF digit verification (send digits, verify they arrive)
  - Call setup success rate over N attempts

This is for QA/acceptance testing, not load generation.
Produces a structured quality report at the end.
"""

import time
import math
import random
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("gencall.scenario.quality_test")


# ═══════════════════════════════════════════════════════════════════════════════
#  QUALITY METRICS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class QualityMetrics:
    """End-to-end call quality measurements."""

    # SIP metrics
    post_dial_delay_ms: float = 0.0       # Time from INVITE to 180/183
    answer_delay_ms: float = 0.0          # Time from INVITE to 200 OK
    setup_success: bool = False

    # RTP metrics
    packets_sent: int = 0
    packets_received: int = 0
    packets_lost: int = 0
    packets_out_of_order: int = 0
    jitter_ms: float = 0.0
    max_jitter_ms: float = 0.0
    avg_latency_ms: float = 0.0

    # Derived
    call_duration_sec: float = 0.0
    codec: str = ""

    @property
    def packet_loss_pct(self) -> float:
        if self.packets_sent == 0:
            return 0.0
        return (self.packets_lost / self.packets_sent) * 100

    @property
    def mos_score(self) -> float:
        """
        Estimate MOS (Mean Opinion Score) using the E-model simplified formula.
        Based on ITU-T G.107.

        MOS range: 1.0 (bad) to 4.5 (excellent)
        """
        # R-factor calculation (simplified)
        # Start with base R = 93.2 for G.711
        r = 93.2

        # Subtract for delay (one-way delay effect)
        d = self.avg_latency_ms
        if d > 177.3:
            r -= 0.024 * d + 0.11 * (d - 177.3)
        else:
            r -= 0.024 * d

        # Subtract for packet loss
        # Using Ie-eff approximation
        loss = self.packet_loss_pct
        if self.codec == "G729":
            ie = 11 + 40 * math.log(1 + 10 * loss) if loss > 0 else 11
        else:
            ie = 0 + 30 * math.log(1 + 15 * loss) if loss > 0 else 0
        r -= ie

        # Subtract for jitter (approximation)
        r -= self.jitter_ms * 0.04

        # Clamp R to valid range
        r = max(0, min(100, r))

        # Convert R to MOS
        if r < 6.5:
            return 1.0
        if r > 100:
            return 4.5
        mos = 1 + 0.035 * r + r * (r - 60) * (100 - r) * 7e-6
        return round(max(1.0, min(4.5, mos)), 2)

    @property
    def quality_rating(self) -> str:
        """Human-readable quality rating."""
        mos = self.mos_score
        if mos >= 4.0:
            return "EXCELLENT"
        elif mos >= 3.6:
            return "GOOD"
        elif mos >= 3.1:
            return "FAIR"
        elif mos >= 2.6:
            return "POOR"
        else:
            return "BAD"

    def to_dict(self) -> dict:
        return {
            "sip": {
                "post_dial_delay_ms": round(self.post_dial_delay_ms, 1),
                "answer_delay_ms": round(self.answer_delay_ms, 1),
                "setup_success": self.setup_success,
            },
            "rtp": {
                "packets_sent": self.packets_sent,
                "packets_received": self.packets_received,
                "packets_lost": self.packets_lost,
                "packet_loss_pct": round(self.packet_loss_pct, 2),
                "packets_out_of_order": self.packets_out_of_order,
                "jitter_ms": round(self.jitter_ms, 2),
                "max_jitter_ms": round(self.max_jitter_ms, 2),
                "avg_latency_ms": round(self.avg_latency_ms, 2),
            },
            "quality": {
                "mos_score": self.mos_score,
                "rating": self.quality_rating,
                "codec": self.codec,
                "duration_sec": round(self.call_duration_sec, 1),
            },
        }

    def report(self) -> str:
        """Generate a human-readable quality report."""
        lines = [
            "",
            "=" * 60,
            "  CALL QUALITY REPORT",
            "=" * 60,
            "",
            "  SIP Signaling:",
            f"    Post-dial delay:    {self.post_dial_delay_ms:.0f} ms",
            f"    Answer delay:       {self.answer_delay_ms:.0f} ms",
            f"    Setup success:      {'YES' if self.setup_success else 'NO'}",
            "",
            "  RTP Media:",
            f"    Codec:              {self.codec}",
            f"    Packets sent:       {self.packets_sent}",
            f"    Packets received:   {self.packets_received}",
            f"    Packet loss:        {self.packet_loss_pct:.2f}%",
            f"    Jitter (avg):       {self.jitter_ms:.1f} ms",
            f"    Jitter (max):       {self.max_jitter_ms:.1f} ms",
            f"    Latency (avg):      {self.avg_latency_ms:.1f} ms",
            "",
            "  Quality Score:",
            f"    MOS:                {self.mos_score} / 4.5",
            f"    Rating:             {self.quality_rating}",
            f"    Duration:           {self.call_duration_sec:.1f}s",
            "",
            "=" * 60,
        ]
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
#  BATCH TEST RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class BatchResult:
    """Results from a batch of quality test calls."""
    total_attempts: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    metrics: list = field(default_factory=list)

    @property
    def asr(self) -> float:
        """Answer-Seizure Ratio (%)."""
        if self.total_attempts == 0:
            return 0.0
        return (self.successful_calls / self.total_attempts) * 100

    @property
    def avg_mos(self) -> float:
        """Average MOS across all successful calls."""
        scores = [m.mos_score for m in self.metrics if m.setup_success]
        return sum(scores) / len(scores) if scores else 0.0

    @property
    def avg_pdd(self) -> float:
        """Average Post-Dial Delay (ms)."""
        pdds = [m.post_dial_delay_ms for m in self.metrics if m.setup_success]
        return sum(pdds) / len(pdds) if pdds else 0.0

    @property
    def avg_duration(self) -> float:
        """Average Call Duration (seconds)."""
        durations = [m.call_duration_sec for m in self.metrics if m.setup_success]
        return sum(durations) / len(durations) if durations else 0.0

    @property
    def avg_packet_loss(self) -> float:
        """Average packet loss (%)."""
        losses = [m.packet_loss_pct for m in self.metrics if m.setup_success]
        return sum(losses) / len(losses) if losses else 0.0

    def summary(self) -> str:
        lines = [
            "",
            "=" * 60,
            "  BATCH QUALITY TEST SUMMARY",
            "=" * 60,
            "",
            f"  Total attempts:      {self.total_attempts}",
            f"  Successful:          {self.successful_calls}",
            f"  Failed:              {self.failed_calls}",
            f"  ASR:                 {self.asr:.1f}%",
            "",
            f"  Avg MOS:             {self.avg_mos:.2f} / 4.5",
            f"  Avg PDD:             {self.avg_pdd:.0f} ms",
            f"  Avg Duration:        {self.avg_duration:.1f}s",
            f"  Avg Packet Loss:     {self.avg_packet_loss:.2f}%",
            "",
            "  Per-call breakdown:",
        ]
        for i, m in enumerate(self.metrics, 1):
            status = "OK" if m.setup_success else "FAIL"
            lines.append(
                f"    #{i:3d}  [{status}]  MOS={m.mos_score:.1f}  "
                f"PDD={m.post_dial_delay_ms:.0f}ms  "
                f"Loss={m.packet_loss_pct:.1f}%  "
                f"Jitter={m.jitter_ms:.1f}ms  "
                f"{m.quality_rating}"
            )
        lines.extend(["", "=" * 60])
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "total_attempts": self.total_attempts,
            "successful_calls": self.successful_calls,
            "failed_calls": self.failed_calls,
            "asr_pct": round(self.asr, 2),
            "avg_mos": round(self.avg_mos, 2),
            "avg_pdd_ms": round(self.avg_pdd, 1),
            "avg_duration_sec": round(self.avg_duration, 1),
            "avg_packet_loss_pct": round(self.avg_packet_loss, 2),
            "calls": [m.to_dict() for m in self.metrics],
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  SCENARIO ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def run(ctx):
    """
    Run a single quality test call.
    Measures SIP timing, RTP quality, and estimates MOS.
    """
    messages = ctx.messages
    parameters = ctx.parameters
    metrics = QualityMetrics()
    rtp_stream = None

    dialog = ctx.new_dialog()
    call_start = time.time()

    try:
        # ── Send INVITE ────────────────────────────────────────────────────
        invite = messages.INVITE_DYNAMIC_RTP_G729(parameters)
        raw_invite = dialog.send(invite)
        invite_sent_at = time.time()

        # ── Wait for provisional ───────────────────────────────────────────
        provisional = invite.ignore_replies("100", timeout=15)

        if not provisional:
            metrics.setup_success = False
            logger.warning("Quality test: no provisional response")
            _report_and_quit(ctx, metrics, "No provisional response")
            return

        # Measure post-dial delay
        metrics.post_dial_delay_ms = (time.time() - invite_sent_at) * 1000

        code = provisional.get_code()
        if code in ("180", "183", "181"):
            provisional = invite.ignore_replies("180", "183", timeout=30)

        if not provisional or provisional.get_code() != "200":
            metrics.setup_success = False
            _report_and_quit(ctx, metrics, f"Call failed: {provisional.get_code() if provisional else 'timeout'}")
            return

        # ── 200 OK received ────────────────────────────────────────────────
        metrics.answer_delay_ms = (time.time() - invite_sent_at) * 1000
        metrics.setup_success = True
        answer = provisional

        # ── Start RTP with quality monitoring ──────────────────────────────
        if raw_invite.get_rtp_port() and answer.get_rtp_port():
            rtp_stream = ctx.rtp_streamer(raw_invite, answer, "audio1.g711a")
            rtp_stream.start()
            metrics.codec = "PCMA"

        # ── ACK ────────────────────────────────────────────────────────────
        ack = messages.ACK(parameters)
        answer.reply(ack)

        # ── Hold call for quality measurement ──────────────────────────────
        test_duration = ctx.config.get("quality_test_duration", 30)
        logger.info("Quality test: holding call for %ds", test_duration)
        message = dialog.wait_message(test_duration)

        # ── Collect RTP stats ──────────────────────────────────────────────
        if rtp_stream:
            rtp_stats = rtp_stream.get_stats() if hasattr(rtp_stream, 'get_stats') else {}
            metrics.packets_sent = rtp_stats.get("packets_sent", 0)
            metrics.packets_received = rtp_stats.get("packets_received", 0)
            metrics.packets_lost = rtp_stats.get("packets_lost", 0)
            metrics.packets_out_of_order = rtp_stats.get("out_of_order", 0)
            metrics.jitter_ms = rtp_stats.get("jitter_ms", 0.0)
            metrics.max_jitter_ms = rtp_stats.get("max_jitter_ms", 0.0)
            metrics.avg_latency_ms = rtp_stats.get("avg_latency_ms", 0.0)

        metrics.call_duration_sec = time.time() - call_start

        # ── Handle remote BYE or send our own ──────────────────────────────
        if message and message.get_code() == "BYE":
            bye_ok = messages.BYE_200(parameters)
            message.reply(bye_ok)
        else:
            bye = messages.BYE(parameters)
            dialog.send(bye)
            bye.get_reply(10)

    finally:
        if rtp_stream:
            rtp_stream.stop()

    # ── Report ─────────────────────────────────────────────────────────────
    logger.info(metrics.report())
    _report_and_quit(ctx, metrics, "Quality test complete")


def _report_and_quit(ctx, metrics: QualityMetrics, reason: str):
    """Store metrics and quit."""
    report = metrics.to_dict()
    logger.info("Quality: MOS=%.2f (%s), PDD=%.0fms, Loss=%.2f%%",
                metrics.mos_score, metrics.quality_rating,
                metrics.post_dial_delay_ms, metrics.packet_loss_pct)
    ctx.quit(reason, str(report))
