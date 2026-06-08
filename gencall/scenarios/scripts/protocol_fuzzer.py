"""
GenCall SIP Protocol Fuzzer - Robustness testing for SIP endpoints.

Generates intentionally malformed SIP messages to test parser robustness,
error handling, and security posture of SIP infrastructure.

===========================================================================
DISCLAIMER: This tool is intended SOLELY for authorised security testing
on systems you own or have explicit written permission to test.
Unauthorised use against third-party systems is illegal and unethical.
Always obtain proper authorisation before running any fuzz tests.
===========================================================================

Each fuzz test is a named test case with expected behaviour (typically a
400 Bad Request, not a crash).  The fuzzer runs all tests and reports
which ones caused unexpected behaviour, classified by severity.
"""

from __future__ import annotations

import datetime
import logging
import random
import socket
import string
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("gencall.scenario.protocol_fuzzer")

_DISCLAIMER = (
    "WARNING: This SIP protocol fuzzer is intended ONLY for authorised "
    "testing on systems you own or have explicit written permission to test. "
    "Unauthorised use is illegal and may violate computer fraud laws."
)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class FuzzSeverity(Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FuzzCategory(Enum):
    HEADER_OVERFLOW = "header_overflow"
    INJECTION = "injection"
    MALFORMED_SYNTAX = "malformed_syntax"
    MISSING_FIELDS = "missing_fields"
    DUPLICATE_HEADERS = "duplicate_headers"
    ENCODING = "encoding"
    SDP_ANOMALY = "sdp_anomaly"
    BUFFER_OVERFLOW = "buffer_overflow"
    LOGIC = "logic"


class FuzzVerdict(Enum):
    PASS = "pass"               # target responded correctly
    UNEXPECTED = "unexpected"   # target responded unexpectedly
    CRASH = "crash"             # target stopped responding
    TIMEOUT = "timeout"         # no response (may be OK for some tests)
    ERROR = "error"             # test harness error


@dataclass
class FuzzTestCase:
    """A single fuzz test definition."""
    test_id: str = ""
    name: str = ""
    description: str = ""
    category: FuzzCategory = FuzzCategory.MALFORMED_SYNTAX
    severity: FuzzSeverity = FuzzSeverity.MEDIUM
    expected_codes: list[int] = field(default_factory=lambda: [400])
    message_builder: Optional[Callable[..., str]] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "severity": self.severity.value,
            "expected_codes": self.expected_codes,
        }


@dataclass
class FuzzResult:
    """Result of running a single fuzz test."""
    test_id: str = ""
    test_name: str = ""
    category: FuzzCategory = FuzzCategory.MALFORMED_SYNTAX
    severity: FuzzSeverity = FuzzSeverity.MEDIUM
    verdict: FuzzVerdict = FuzzVerdict.PASS
    response_code: int = 0
    response_reason: str = ""
    response_time_ms: float = 0.0
    expected_codes: list[int] = field(default_factory=list)
    detail: str = ""
    raw_response: str = ""
    timestamp: float = field(default_factory=time.time)

    @property
    def passed(self) -> bool:
        return self.verdict == FuzzVerdict.PASS

    def to_dict(self) -> dict:
        return {
            "test_id": self.test_id,
            "test_name": self.test_name,
            "category": self.category.value,
            "severity": self.severity.value,
            "verdict": self.verdict.value,
            "passed": self.passed,
            "response_code": self.response_code,
            "response_reason": self.response_reason,
            "response_time_ms": round(self.response_time_ms, 2),
            "expected_codes": self.expected_codes,
            "detail": self.detail,
            "timestamp": round(self.timestamp, 3),
        }


@dataclass
class FuzzReport:
    """Aggregated report from a fuzz run."""
    report_id: str = ""
    target: str = ""
    target_port: int = 5060
    started_at: Optional[datetime.datetime] = None
    completed_at: Optional[datetime.datetime] = None
    duration_seconds: float = 0.0
    total_tests: int = 0
    passed: int = 0
    unexpected: int = 0
    crashes: int = 0
    timeouts: int = 0
    errors: int = 0
    results: list[FuzzResult] = field(default_factory=list)
    disclaimer: str = _DISCLAIMER

    def __post_init__(self) -> None:
        if not self.report_id:
            self.report_id = uuid.uuid4().hex[:12]

    @property
    def findings(self) -> list[FuzzResult]:
        """Return only non-passing results."""
        return [r for r in self.results if not r.passed]

    @property
    def critical_findings(self) -> list[FuzzResult]:
        return [
            r for r in self.results
            if not r.passed and r.severity in (FuzzSeverity.HIGH, FuzzSeverity.CRITICAL)
        ]

    def to_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "disclaimer": self.disclaimer,
            "target": self.target,
            "target_port": self.target_port,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": round(self.duration_seconds, 2),
            "total_tests": self.total_tests,
            "passed": self.passed,
            "unexpected": self.unexpected,
            "crashes": self.crashes,
            "timeouts": self.timeouts,
            "errors": self.errors,
            "findings_count": len(self.findings),
            "critical_findings_count": len(self.critical_findings),
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# SIP message helpers
# ---------------------------------------------------------------------------

def _branch() -> str:
    return f"z9hG4bK{uuid.uuid4().hex[:16]}"


def _tag() -> str:
    return uuid.uuid4().hex[:8]


def _call_id() -> str:
    return f"{uuid.uuid4().hex[:16]}@gencall-fuzz"


def _base_options(target: str, port: int, local_ip: str, local_port: int) -> str:
    """Build a valid baseline OPTIONS message."""
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {local_ip}:{local_port};branch={_branch()};rport\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{local_ip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}:{port}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Contact: <sip:fuzz@{local_ip}:{local_port}>\r\n"
        f"Content-Length: 0\r\n"
        f"User-Agent: GenCall/2.0 ProtocolFuzzer\r\n"
        f"\r\n"
    )


# ---------------------------------------------------------------------------
# Fuzz test builders
# ---------------------------------------------------------------------------

def _build_oversized_via(target: str, port: int, lip: str, lp: int) -> str:
    huge_via = "a" * 8000
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {huge_via};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_null_bytes_in_header(target: str, port: int, lip: str, lp: int) -> str:
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz\x00injected@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_invalid_utf8(target: str, port: int, lip: str, lp: int) -> str:
    bad_utf8 = b"\xfe\xff\x80\x81"
    name = "fuzz" + bad_utf8.decode("latin-1")
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: \"{name}\" <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_missing_call_id(target: str, port: int, lip: str, lp: int) -> str:
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_missing_cseq(target: str, port: int, lip: str, lp: int) -> str:
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_missing_via(target: str, port: int, lip: str, lp: int) -> str:
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_duplicate_via(target: str, port: int, lip: str, lp: int) -> str:
    via = f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        + via * 50
        + f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_duplicate_call_id(target: str, port: int, lip: str, lp: int) -> str:
    cid1 = _call_id()
    cid2 = _call_id()
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {cid1}\r\n"
        f"Call-ID: {cid2}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_enormous_cseq(target: str, port: int, lip: str, lp: int) -> str:
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 99999999999999999999 OPTIONS\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_malformed_via_branch(target: str, port: int, lip: str, lp: int) -> str:
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch=NOT-A-MAGIC-COOKIE\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_broken_sdp(target: str, port: int, lip: str, lp: int) -> str:
    bad_sdp = (
        "v=INVALID\r\n"
        "o=fuzz 0 0 IN IP4 0.0.0.0\r\n"
        "s=\r\n"
        "c=IN IP4\r\n"
        "t=0 0\r\n"
        "m=audio NOTANUMBER RTP/AVP 0\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    )
    return (
        f"INVITE sip:fuzz@{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:fuzz@{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:fuzz@{lip}:{lp}>\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(bad_sdp)}\r\n"
        f"\r\n"
        f"{bad_sdp}"
    )


def _build_negative_content_length(target: str, port: int, lip: str, lp: int) -> str:
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Length: -1\r\n\r\n"
    )


def _build_uri_overflow(target: str, port: int, lip: str, lp: int) -> str:
    huge_user = "A" * 4000
    return (
        f"OPTIONS sip:{huge_user}@{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_invalid_method(target: str, port: int, lip: str, lp: int) -> str:
    return (
        f"FUZZMETHOD sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 FUZZMETHOD\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_wrong_content_length(target: str, port: int, lip: str, lp: int) -> str:
    body = "short"
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: 99999\r\n"
        f"\r\n"
        f"{body}"
    )


def _build_crlf_injection(target: str, port: int, lip: str, lp: int) -> str:
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"X-Injected: value\r\nX-Evil: injected\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_max_forwards_zero(target: str, port: int, lip: str, lp: int) -> str:
    return (
        f"OPTIONS sip:{target}:{port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Max-Forwards: 0\r\n"
        f"From: <sip:fuzz@{lip}>;tag={_tag()}\r\n"
        f"To: <sip:{target}>\r\n"
        f"Call-ID: {_call_id()}\r\n"
        f"CSeq: 1 OPTIONS\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_garbage_start_line(target: str, port: int, lip: str, lp: int) -> str:
    return (
        f"THIS IS NOT SIP AT ALL\r\n"
        f"Via: SIP/2.0/UDP {lip}:{lp};branch={_branch()}\r\n"
        f"Content-Length: 0\r\n\r\n"
    )


def _build_empty_message(target: str, port: int, lip: str, lp: int) -> str:
    return "\r\n\r\n"


def _build_binary_garbage(target: str, port: int, lip: str, lp: int) -> str:
    return "".join(chr(random.randint(0, 255)) for _ in range(500))


# ---------------------------------------------------------------------------
# Test case registry
# ---------------------------------------------------------------------------

_DEFAULT_TEST_CASES: list[FuzzTestCase] = [
    FuzzTestCase(
        test_id="FUZZ-001",
        name="Oversized Via Header",
        description="Via header with 8000-byte host field to test header buffer handling",
        category=FuzzCategory.HEADER_OVERFLOW,
        severity=FuzzSeverity.HIGH,
        expected_codes=[400, 413, 500],
        message_builder=_build_oversized_via,
    ),
    FuzzTestCase(
        test_id="FUZZ-002",
        name="Null Bytes in From Header",
        description="Null byte injection in From header URI",
        category=FuzzCategory.INJECTION,
        severity=FuzzSeverity.HIGH,
        expected_codes=[400],
        message_builder=_build_null_bytes_in_header,
    ),
    FuzzTestCase(
        test_id="FUZZ-003",
        name="Invalid UTF-8 in Display Name",
        description="Non-UTF-8 bytes in From display name",
        category=FuzzCategory.ENCODING,
        severity=FuzzSeverity.MEDIUM,
        expected_codes=[200, 400],
        message_builder=_build_invalid_utf8,
    ),
    FuzzTestCase(
        test_id="FUZZ-004",
        name="Missing Call-ID",
        description="OPTIONS request without required Call-ID header",
        category=FuzzCategory.MISSING_FIELDS,
        severity=FuzzSeverity.MEDIUM,
        expected_codes=[400],
        message_builder=_build_missing_call_id,
    ),
    FuzzTestCase(
        test_id="FUZZ-005",
        name="Missing CSeq",
        description="OPTIONS request without required CSeq header",
        category=FuzzCategory.MISSING_FIELDS,
        severity=FuzzSeverity.MEDIUM,
        expected_codes=[400],
        message_builder=_build_missing_cseq,
    ),
    FuzzTestCase(
        test_id="FUZZ-006",
        name="Missing Via",
        description="OPTIONS request without required Via header",
        category=FuzzCategory.MISSING_FIELDS,
        severity=FuzzSeverity.MEDIUM,
        expected_codes=[400],
        message_builder=_build_missing_via,
    ),
    FuzzTestCase(
        test_id="FUZZ-007",
        name="50 Duplicate Via Headers",
        description="Message with 50 identical Via headers",
        category=FuzzCategory.DUPLICATE_HEADERS,
        severity=FuzzSeverity.MEDIUM,
        expected_codes=[200, 400, 483],
        message_builder=_build_duplicate_via,
    ),
    FuzzTestCase(
        test_id="FUZZ-008",
        name="Duplicate Call-ID Headers",
        description="Two different Call-ID headers in same message",
        category=FuzzCategory.DUPLICATE_HEADERS,
        severity=FuzzSeverity.LOW,
        expected_codes=[200, 400],
        message_builder=_build_duplicate_call_id,
    ),
    FuzzTestCase(
        test_id="FUZZ-009",
        name="Enormous CSeq Number",
        description="CSeq with 20-digit number far exceeding 32-bit range",
        category=FuzzCategory.LOGIC,
        severity=FuzzSeverity.HIGH,
        expected_codes=[200, 400, 500],
        message_builder=_build_enormous_cseq,
    ),
    FuzzTestCase(
        test_id="FUZZ-010",
        name="Malformed Via Branch",
        description="Via branch without magic cookie z9hG4bK prefix",
        category=FuzzCategory.MALFORMED_SYNTAX,
        severity=FuzzSeverity.LOW,
        expected_codes=[200, 400],
        message_builder=_build_malformed_via_branch,
    ),
    FuzzTestCase(
        test_id="FUZZ-011",
        name="Broken SDP Body",
        description="INVITE with invalid SDP (non-numeric port, bad version)",
        category=FuzzCategory.SDP_ANOMALY,
        severity=FuzzSeverity.MEDIUM,
        expected_codes=[400, 488, 500, 606],
        message_builder=_build_broken_sdp,
    ),
    FuzzTestCase(
        test_id="FUZZ-012",
        name="Negative Content-Length",
        description="Content-Length header set to -1",
        category=FuzzCategory.MALFORMED_SYNTAX,
        severity=FuzzSeverity.HIGH,
        expected_codes=[400],
        message_builder=_build_negative_content_length,
    ),
    FuzzTestCase(
        test_id="FUZZ-013",
        name="URI Buffer Overflow Attempt",
        description="Request-URI with 4000-character user part",
        category=FuzzCategory.BUFFER_OVERFLOW,
        severity=FuzzSeverity.CRITICAL,
        expected_codes=[400, 414, 500],
        message_builder=_build_uri_overflow,
    ),
    FuzzTestCase(
        test_id="FUZZ-014",
        name="Unknown SIP Method",
        description="Request with non-existent method FUZZMETHOD",
        category=FuzzCategory.LOGIC,
        severity=FuzzSeverity.LOW,
        expected_codes=[405, 501],
        message_builder=_build_invalid_method,
    ),
    FuzzTestCase(
        test_id="FUZZ-015",
        name="Content-Length Mismatch",
        description="Content-Length claims 99999 bytes but body is 5 bytes",
        category=FuzzCategory.MALFORMED_SYNTAX,
        severity=FuzzSeverity.HIGH,
        expected_codes=[400],
        message_builder=_build_wrong_content_length,
    ),
    FuzzTestCase(
        test_id="FUZZ-016",
        name="CRLF Header Injection",
        description="Attempt to inject extra headers via CRLF in header value",
        category=FuzzCategory.INJECTION,
        severity=FuzzSeverity.HIGH,
        expected_codes=[200, 400],
        message_builder=_build_crlf_injection,
    ),
    FuzzTestCase(
        test_id="FUZZ-017",
        name="Max-Forwards: 0",
        description="Max-Forwards set to 0 - should get 483 Too Many Hops",
        category=FuzzCategory.LOGIC,
        severity=FuzzSeverity.LOW,
        expected_codes=[200, 483],
        message_builder=_build_max_forwards_zero,
    ),
    FuzzTestCase(
        test_id="FUZZ-018",
        name="Garbage Start Line",
        description="Completely invalid first line - not SIP at all",
        category=FuzzCategory.MALFORMED_SYNTAX,
        severity=FuzzSeverity.MEDIUM,
        expected_codes=[400],
        message_builder=_build_garbage_start_line,
    ),
    FuzzTestCase(
        test_id="FUZZ-019",
        name="Empty Message",
        description="Entirely empty SIP message (just CRLF)",
        category=FuzzCategory.MALFORMED_SYNTAX,
        severity=FuzzSeverity.MEDIUM,
        expected_codes=[400],
        message_builder=_build_empty_message,
    ),
    FuzzTestCase(
        test_id="FUZZ-020",
        name="Binary Garbage",
        description="500 bytes of random binary data",
        category=FuzzCategory.BUFFER_OVERFLOW,
        severity=FuzzSeverity.HIGH,
        expected_codes=[400],
        message_builder=_build_binary_garbage,
    ),
]


# ---------------------------------------------------------------------------
# Protocol Fuzzer
# ---------------------------------------------------------------------------

class ProtocolFuzzer:
    """
    SIP Protocol Fuzzer for robustness testing.

    =========================================================================
    IMPORTANT: Only use on systems you own or have explicit authorisation
    to test.  Unauthorised use may violate computer fraud laws.
    =========================================================================

    Usage::

        fuzzer = ProtocolFuzzer("pbx.example.com", 5060)
        report = fuzzer.run_all()
        print(f"Findings: {len(report.findings)}")
    """

    def __init__(
        self,
        target: str,
        port: int = 5060,
        local_ip: str = "0.0.0.0",
        local_port: int = 0,
        timeout: float = 5.0,
        delay_between_tests: float = 0.5,
        test_cases: Optional[list[FuzzTestCase]] = None,
        progress_callback: Optional[Callable[[int, int, FuzzResult], None]] = None,
    ) -> None:
        self.target = target
        self.port = port
        self.local_ip = local_ip
        self.local_port = local_port
        self.timeout = timeout
        self.delay_between_tests = delay_between_tests
        self._test_cases = test_cases or list(_DEFAULT_TEST_CASES)
        self._callback = progress_callback

        logger.warning(_DISCLAIMER)
        logger.info(
            "ProtocolFuzzer created for %s:%d (%d test cases)",
            target, port, len(self._test_cases),
        )

    @property
    def test_cases(self) -> list[FuzzTestCase]:
        return self._test_cases

    def add_test_case(self, tc: FuzzTestCase) -> None:
        self._test_cases.append(tc)

    def run_all(self) -> FuzzReport:
        """Run all fuzz tests sequentially and return the report."""
        report = FuzzReport(
            target=self.target,
            target_port=self.port,
            started_at=datetime.datetime.utcnow(),
            total_tests=len(self._test_cases),
        )

        # Verify target is reachable with a valid message first
        if not self._health_check():
            logger.warning("Target %s:%d is not responding to valid SIP; results may be unreliable", self.target, self.port)

        for i, tc in enumerate(self._test_cases):
            result = self._run_test(tc)
            report.results.append(result)

            if result.verdict == FuzzVerdict.PASS:
                report.passed += 1
            elif result.verdict == FuzzVerdict.UNEXPECTED:
                report.unexpected += 1
            elif result.verdict == FuzzVerdict.CRASH:
                report.crashes += 1
            elif result.verdict == FuzzVerdict.TIMEOUT:
                report.timeouts += 1
            else:
                report.errors += 1

            if self._callback:
                try:
                    self._callback(i + 1, len(self._test_cases), result)
                except Exception:
                    pass

            if self.delay_between_tests > 0:
                time.sleep(self.delay_between_tests)

        report.completed_at = datetime.datetime.utcnow()
        if report.started_at and report.completed_at:
            report.duration_seconds = (report.completed_at - report.started_at).total_seconds()

        logger.info(
            "Fuzz run complete: %d tests, %d passed, %d unexpected, %d crashes, %d timeouts",
            report.total_tests, report.passed, report.unexpected,
            report.crashes, report.timeouts,
        )
        return report

    def run_single(self, test_id: str) -> Optional[FuzzResult]:
        """Run a single test by ID."""
        for tc in self._test_cases:
            if tc.test_id == test_id:
                return self._run_test(tc)
        logger.warning("Test case %s not found", test_id)
        return None

    def _run_test(self, tc: FuzzTestCase) -> FuzzResult:
        """Execute a single fuzz test case."""
        result = FuzzResult(
            test_id=tc.test_id,
            test_name=tc.name,
            category=tc.category,
            severity=tc.severity,
            expected_codes=tc.expected_codes,
        )

        try:
            # Discover local IP if needed
            lip = self.local_ip
            if lip in ("0.0.0.0", "::"):
                lip = self._discover_ip()

            # Build the fuzz message
            if tc.message_builder is None:
                result.verdict = FuzzVerdict.ERROR
                result.detail = "No message builder"
                return result

            msg = tc.message_builder(self.target, self.port, lip, self.local_port or 5060)

            # Send and receive
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            if self.local_port:
                sock.bind((self.local_ip, 0))
            else:
                sock.bind((self.local_ip, 0))

            send_time = time.monotonic()
            data = msg.encode("utf-8", errors="replace") if isinstance(msg, str) else msg
            sock.sendto(data, (self.target, self.port))

            try:
                resp_data, addr = sock.recvfrom(65535)
                recv_time = time.monotonic()
                result.response_time_ms = (recv_time - send_time) * 1000.0
                result.raw_response = resp_data.decode("utf-8", errors="replace")

                # Parse response code
                first_line = result.raw_response.split("\r\n", 1)[0].split("\n", 1)[0]
                parts = first_line.split(None, 2)
                if len(parts) >= 2 and parts[0].startswith("SIP/"):
                    result.response_code = int(parts[1])
                    result.response_reason = parts[2] if len(parts) > 2 else ""

                # Evaluate
                if result.response_code in tc.expected_codes:
                    result.verdict = FuzzVerdict.PASS
                    result.detail = f"Got expected {result.response_code}"
                else:
                    result.verdict = FuzzVerdict.UNEXPECTED
                    result.detail = (
                        f"Got {result.response_code} but expected one of {tc.expected_codes}"
                    )

            except socket.timeout:
                result.verdict = FuzzVerdict.TIMEOUT
                result.detail = f"No response within {self.timeout}s"

                # After a timeout, check if the target is still alive
                if not self._health_check():
                    result.verdict = FuzzVerdict.CRASH
                    result.detail = "Target stopped responding after this test"
                    result.severity = FuzzSeverity.CRITICAL

            sock.close()

        except Exception as exc:
            result.verdict = FuzzVerdict.ERROR
            result.detail = str(exc)
            logger.debug("Fuzz test %s error: %s", tc.test_id, exc, exc_info=True)

        logger.info(
            "Fuzz %s (%s): %s - %s",
            tc.test_id, tc.name, result.verdict.value, result.detail,
        )
        return result

    def _health_check(self) -> bool:
        """Send a valid OPTIONS and check for any response."""
        try:
            lip = self.local_ip if self.local_ip not in ("0.0.0.0", "::") else self._discover_ip()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(3.0)
            sock.bind((self.local_ip, 0))
            lp = sock.getsockname()[1]
            msg = _base_options(self.target, self.port, lip, lp)
            sock.sendto(msg.encode("utf-8"), (self.target, self.port))
            sock.recvfrom(65535)
            sock.close()
            return True
        except Exception:
            return False

    @staticmethod
    def _discover_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 1))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"
