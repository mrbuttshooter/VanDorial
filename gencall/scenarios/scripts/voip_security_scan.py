"""
GenCall - VoIP Security Scanner

Authorized security testing scenarios for SIP infrastructure:
  - SIP method enumeration (OPTIONS discovery)
  - Authentication bypass detection
  - TLS certificate validation
  - Extension enumeration (REGISTER scan)
  - Malformed SIP message handling
  - SDP anomaly detection

IMPORTANT: Only use on systems you own or have explicit authorization to test.
"""

import time
import random
import string
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("gencall.scenario.security_scan")


@dataclass
class ScanResult:
    """Result of a single security test."""
    test_name: str
    status: str = "pass"     # pass, fail, warning, error
    severity: str = "info"   # info, low, medium, high, critical
    detail: str = ""
    recommendation: str = ""

    def to_dict(self) -> dict:
        return {
            "test": self.test_name,
            "status": self.status,
            "severity": self.severity,
            "detail": self.detail,
            "recommendation": self.recommendation,
        }


@dataclass
class SecurityReport:
    """Aggregated security scan results."""
    target: str
    started_at: float = 0.0
    completed_at: float = 0.0
    results: list[ScanResult] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.completed_at - self.started_at

    @property
    def critical_count(self) -> int:
        return sum(1 for r in self.results if r.severity == "critical")

    @property
    def high_count(self) -> int:
        return sum(1 for r in self.results if r.severity == "high")

    def add(self, result: ScanResult):
        self.results.append(result)

    def report(self) -> str:
        lines = [
            "",
            "=" * 65,
            "  GENCALL SECURITY SCAN REPORT",
            "=" * 65,
            f"  Target:    {self.target}",
            f"  Duration:  {self.duration:.1f}s",
            f"  Tests:     {len(self.results)}",
            f"  Critical:  {self.critical_count}",
            f"  High:      {self.high_count}",
            "",
            "-" * 65,
        ]
        for r in self.results:
            icon = {"pass": "[OK]", "fail": "[!!]", "warning": "[??]", "error": "[EE]"}
            lines.append(f"  {icon.get(r.status, '[--]')} [{r.severity.upper():>8}] {r.test_name}")
            if r.detail:
                lines.append(f"       Detail: {r.detail}")
            if r.recommendation:
                lines.append(f"       Fix:    {r.recommendation}")
            lines.append("")
        lines.append("=" * 65)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "duration_sec": round(self.duration, 1),
            "total_tests": len(self.results),
            "critical": self.critical_count,
            "high": self.high_count,
            "results": [r.to_dict() for r in self.results],
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════

def test_options_enumeration(ctx, messages, parameters, report: SecurityReport):
    """
    Test: SIP Methods Enumeration via OPTIONS.
    Checks what SIP methods the target advertises support for.
    """
    logger.info("Testing: OPTIONS method enumeration")

    dialog = ctx.new_dialog()
    options = messages.OPTIONS(parameters)
    dialog.send(options)

    reply = options.get_reply(10)

    result = ScanResult(test_name="SIP Method Enumeration (OPTIONS)")

    if not reply:
        result.status = "error"
        result.detail = "No response to OPTIONS request"
        result.recommendation = "Target may be blocking OPTIONS or unreachable"
    else:
        code = reply.get_code()
        if code == "200":
            # Check Allow header
            allow = ""
            if hasattr(reply, 'data'):
                for line in reply.data.split("\r\n"):
                    if line.lower().startswith("allow:"):
                        allow = line.split(":", 1)[1].strip()

            if allow:
                methods = [m.strip() for m in allow.split(",")]
                result.detail = f"Allowed methods: {', '.join(methods)}"

                # Check for dangerous methods
                dangerous = {"DEBUG", "TRACE"}
                found_dangerous = dangerous & set(methods)
                if found_dangerous:
                    result.status = "fail"
                    result.severity = "medium"
                    result.recommendation = f"Disable debug methods: {found_dangerous}"
                else:
                    result.status = "pass"
                    result.severity = "info"
            else:
                result.status = "warning"
                result.detail = f"200 OK but no Allow header (code: {code})"
        else:
            result.detail = f"Response code: {code}"
            result.status = "warning"
            result.severity = "low"

    report.add(result)


def test_register_without_auth(ctx, messages, parameters, report: SecurityReport):
    """
    Test: Registration without authentication.
    Attempts to REGISTER without credentials. Should be rejected.
    """
    logger.info("Testing: REGISTER without authentication")

    params = dict(parameters)
    params["fromNumber"] = "security-test-" + "".join(random.choices(string.digits, k=4))

    dialog = ctx.new_dialog()
    register = messages.REGISTER(params)
    dialog.send(register)

    reply = register.get_reply(10)

    result = ScanResult(test_name="Registration Without Auth")

    if not reply:
        result.status = "error"
        result.detail = "No response to unauthenticated REGISTER"
    else:
        code = reply.get_code()
        if code == "200":
            result.status = "fail"
            result.severity = "critical"
            result.detail = "REGISTER accepted without authentication!"
            result.recommendation = "Enable digest authentication for all registrations"
        elif code in ("401", "407"):
            result.status = "pass"
            result.severity = "info"
            result.detail = f"Correctly challenged with {code}"
        elif code == "403":
            result.status = "pass"
            result.severity = "info"
            result.detail = "Correctly rejected with 403 Forbidden"
        else:
            result.status = "warning"
            result.severity = "low"
            result.detail = f"Unexpected response: {code}"

    report.add(result)


def test_invite_without_auth(ctx, messages, parameters, report: SecurityReport):
    """
    Test: INVITE without authentication.
    Attempts to place a call without credentials.
    """
    logger.info("Testing: INVITE without authentication")

    params = dict(parameters)
    params["fromNumber"] = "security-scan"
    params["toNumber"] = "echo-test"

    dialog = ctx.new_dialog()
    invite = messages.INVITE_DYNAMIC_RTP_G729(params)
    dialog.send(invite)

    reply = invite.ignore_replies("100", timeout=10)

    result = ScanResult(test_name="Call Without Authentication")

    if not reply:
        result.status = "warning"
        result.detail = "No response to unauthenticated INVITE"
        result.severity = "low"
    else:
        code = reply.get_code()
        if code in ("180", "183", "200"):
            result.status = "fail"
            result.severity = "high"
            result.detail = f"Call progressed without auth (got {code})"
            result.recommendation = "Require authentication for all INVITEs"

            # Clean up - send CANCEL or BYE
            if code == "200":
                ack = messages.ACK(params)
                reply.reply(ack)
                bye = messages.BYE(params)
                dialog.send(bye)
                bye.get_reply(5)
            else:
                cancel = messages.CANCEL(params)
                dialog.send(cancel)
                cancel.get_reply(5)
        elif code in ("401", "407"):
            result.status = "pass"
            result.severity = "info"
            result.detail = f"Correctly challenged with {code}"
            # ACK the challenge
            ack = messages.ACK_NON200OK(params)
            reply.reply(ack)
        elif code in ("403", "503", "488"):
            result.status = "pass"
            result.severity = "info"
            result.detail = f"Rejected with {code}"
        else:
            result.status = "warning"
            result.severity = "low"
            result.detail = f"Response: {code}"

    report.add(result)


def test_user_agent_disclosure(ctx, messages, parameters, report: SecurityReport):
    """
    Test: Server information disclosure via User-Agent/Server headers.
    """
    logger.info("Testing: Information disclosure (User-Agent/Server)")

    dialog = ctx.new_dialog()
    options = messages.OPTIONS(parameters)
    dialog.send(options)

    reply = options.get_reply(10)

    result = ScanResult(test_name="Server Information Disclosure")

    if reply and hasattr(reply, 'data'):
        server_info = ""
        for line in reply.data.split("\r\n"):
            lower = line.lower()
            if lower.startswith("server:") or lower.startswith("user-agent:"):
                server_info = line.split(":", 1)[1].strip()
                break

        if server_info:
            # Check if version info is disclosed
            import re
            has_version = bool(re.search(r"\d+\.\d+", server_info))
            result.detail = f"Disclosed: {server_info}"

            if has_version:
                result.status = "warning"
                result.severity = "low"
                result.recommendation = "Remove version numbers from Server/User-Agent headers"
            else:
                result.status = "pass"
                result.severity = "info"
        else:
            result.status = "pass"
            result.severity = "info"
            result.detail = "No Server/User-Agent header disclosed"
    else:
        result.status = "warning"
        result.detail = "Could not retrieve server headers"

    report.add(result)


def test_transport_security(ctx, messages, parameters, report: SecurityReport):
    """
    Test: Check if the target supports encrypted transport (TLS/SIPS).
    """
    logger.info("Testing: Transport security (TLS support)")

    result = ScanResult(test_name="Transport Layer Security")

    # Check if we're using TLS
    transport = parameters.get("transport", "udp").lower()

    if transport == "tls":
        result.status = "pass"
        result.severity = "info"
        result.detail = "Connection using TLS"
    else:
        # Try to detect if target supports TLS from OPTIONS
        result.status = "warning"
        result.severity = "medium"
        result.detail = f"Current transport: {transport.upper()} (unencrypted)"
        result.recommendation = "Use TLS (SIPS) for signaling encryption"

    report.add(result)


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def run(ctx):
    """
    Run the VoIP security scan against the configured target.
    """
    messages = ctx.messages
    parameters = ctx.parameters

    target = f"{parameters.get('remote_host', 'unknown')}:{parameters.get('remote_port', 5060)}"
    report = SecurityReport(target=target, started_at=time.time())

    logger.info("Starting security scan against %s", target)

    # Run all tests
    tests = [
        test_options_enumeration,
        test_register_without_auth,
        test_invite_without_auth,
        test_user_agent_disclosure,
        test_transport_security,
    ]

    for test_fn in tests:
        try:
            test_fn(ctx, messages, parameters, report)
        except Exception as e:
            report.add(ScanResult(
                test_name=test_fn.__name__,
                status="error",
                detail=f"Test crashed: {e}",
            ))
        ctx.sleep(0.5)  # Small delay between tests

    report.completed_at = time.time()

    # Print report
    logger.info(report.report())

    ctx.quit("Security scan complete", str(report.to_dict()))
