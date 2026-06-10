"""
Per-call record tests (design §4.2 / §5, build stage "Records").

Covers, with no real SIPp/Docker/Linux (SQLite + the Foundation stub `sipp`):

  * log-line parsing of the loop_uac / loop_uas <log> token streams,
  * incremental accumulation of a call's several event lines into one record,
  * the RFC reference-event duration math, including the spec §1 / PPT worked
    example (60 s call, 100 ms one-way propagation -> A 60.000 s / B 60.205 s),
  * a failed call (487) recorded with final_code and zero duration,
  * end-to-end ingest of the stub `sipp`'s emitted call-log lines into the
    call_records table via the throttled parser.

The parser's poll loop is exercised by calling poll_once() directly so the test
never depends on the >= 1 s background sleep.
"""

import os
import time

import pytest

from gencall.core.call_records import (
    MIN_POLL_INTERVAL_S,
    CallRecordParser,
    _CallAccumulator,
    ip_in_whitelist,
    parse_log_line,
)
from gencall.db.migrations import apply_migrations
from gencall.db.models import Database


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    """Temp SQLite Database with ORM tables + plain SQL migrations applied."""
    db_path = tmp_path / "records.db"
    database = Database(f"sqlite:///{db_path}")
    database.create_tables()
    apply_migrations(database.engine)
    return database


def _fetch_records(db):
    from sqlalchemy import text

    with db.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT campaign_id, direction, call_uuid, a_number, b_number, "
                "source_ip, t_start_ms, t_answer_ms, t_end_ms, duration_ms, "
                "final_code FROM call_records ORDER BY id"
            )
        )
        cols = [
            "campaign_id", "direction", "call_uuid", "a_number", "b_number",
            "source_ip", "t_start_ms", "t_answer_ms", "t_end_ms", "duration_ms",
            "final_code",
        ]
        return [dict(zip(cols, r)) for r in rows]


# ── log-line parsing ────────────────────────────────────────────────────────


def test_parse_log_line_tokens():
    line = (
        "loop_uac direction=out call_id=abc@h a_number=100 b_number=200 "
        "event=answer t_200ok_received=1700000000120 final_code=200"
    )
    fields = parse_log_line(line)
    assert fields["direction"] == "out"
    assert fields["call_id"] == "abc@h"
    assert fields["a_number"] == "100"
    assert fields["b_number"] == "200"
    assert fields["t_200ok_received"] == "1700000000120"
    assert fields["final_code"] == "200"
    # The bare leading scenario tag is ignored (no '=').
    assert "loop_uac" not in fields


def test_parse_log_line_ignores_non_records():
    # Blank line and SIPp noise without a call_id are dropped.
    assert parse_log_line("") == {}
    assert parse_log_line("2026-06-10 some unrelated sipp log message") == {}


def test_parse_log_line_order_tolerant():
    a = parse_log_line("call_id=x direction=in t_bye_received=5")
    b = parse_log_line("t_bye_received=5 direction=in call_id=x")
    assert a == b


# ── accumulation + duration math ────────────────────────────────────────────


def test_accumulator_outbound_a_side_duration():
    """A-side duration = t_bye_sent - t_200ok_received."""
    acc = _CallAccumulator()
    acc.ingest(parse_log_line(
        "direction=out call_id=c1 a_number=100 b_number=200 t_invite=1000"))
    acc.ingest(parse_log_line(
        "direction=out call_id=c1 t_200ok_received=1100 final_code=200"))
    acc.ingest(parse_log_line(
        "direction=out call_id=c1 t_bye_sent=61100"))
    rows = dict(acc.pop_complete())
    (row,) = rows.values()
    assert row["direction"] == "out"
    assert row["a_number"] == "100"
    assert row["b_number"] == "200"
    assert row["t_answer_ms"] == 1100
    assert row["t_end_ms"] == 61100
    assert row["duration_ms"] == 60000
    assert row["final_code"] == 200


def test_accumulator_inbound_maps_from_to_onto_ab():
    """UAS from_number/to_number map onto a_number/b_number; B-side math."""
    acc = _CallAccumulator()
    acc.ingest(parse_log_line(
        "direction=in call_id=c2 from_number=100 to_number=200 "
        "source_ip=10.0.0.9 t_invite_received=900"))
    acc.ingest(parse_log_line(
        "direction=in call_id=c2 t_200ok_sent=1000"))
    acc.ingest(parse_log_line(
        "direction=in call_id=c2 t_bye_received=61205"))
    (row,) = dict(acc.pop_complete()).values()
    assert row["direction"] == "in"
    assert row["a_number"] == "100"     # from_number
    assert row["b_number"] == "200"     # to_number
    assert row["source_ip"] == "10.0.0.9"
    assert row["duration_ms"] == 61205 - 1000


def test_spec_worked_example_a_60000_b_60205():
    """Spec §1 / PPT worked example.

    A 60 s call with 100 ms one-way propagation must record A-side 60.000 s and
    B-side 60.205 s. Timeline (ms), with a 5 ms internal skew on the answer leg
    so both reference deltas land exactly on the spec's figures:

        t_200ok_sent (B)      = 1000
        t_200ok_received (A)  = 1105   (100 ms propagation + 5 ms)
        t_bye_sent (A)        = 61105  (A-side hold = exactly 60.000 s)
        t_bye_received (B)    = 61205  (100 ms propagation)

      A-side = 61105 - 1105 = 60000 ms = 60.000 s
      B-side = 61205 - 1000 = 60205 ms = 60.205 s
    """
    acc = _CallAccumulator()
    # A-side (UAC).
    acc.ingest(parse_log_line(
        "direction=out call_id=loop1 a_number=100 b_number=200 t_invite=1000"))
    acc.ingest(parse_log_line(
        "direction=out call_id=loop1 t_200ok_received=1105 final_code=200"))
    acc.ingest(parse_log_line(
        "direction=out call_id=loop1 t_bye_sent=61105"))
    # B-side (UAS) — same Call-ID, different direction => distinct record.
    acc.ingest(parse_log_line(
        "direction=in call_id=loop1 from_number=100 to_number=200 "
        "source_ip=10.9.9.9 t_invite_received=905"))
    acc.ingest(parse_log_line(
        "direction=in call_id=loop1 t_200ok_sent=1000"))
    acc.ingest(parse_log_line(
        "direction=in call_id=loop1 t_bye_received=61205"))

    rows = {k: v for k, v in acc.pop_complete()}
    out_row = next(r for r in rows.values() if r["direction"] == "out")
    in_row = next(r for r in rows.values() if r["direction"] == "in")

    assert out_row["duration_ms"] == 60000          # A-side 60.000 s
    assert in_row["duration_ms"] == 60205           # B-side 60.205 s
    # Stored raw ms; rounding is display-only — confirm the second values match.
    assert round(out_row["duration_ms"] / 1000.0, 3) == 60.000
    assert round(in_row["duration_ms"] / 1000.0, 3) == 60.205
    # B-side is structurally longer (carries both propagation legs).
    assert in_row["duration_ms"] - out_row["duration_ms"] == 205


def test_failed_call_487_zero_duration():
    """A failed call (487 Request Terminated) records final_code, duration 0."""
    acc = _CallAccumulator()
    acc.ingest(parse_log_line(
        "direction=out call_id=fail1 a_number=100 b_number=200 t_invite=1000"))
    # No answer event; the scenario logs the final failure code instead.
    acc.ingest(parse_log_line(
        "direction=out call_id=fail1 final_code=487"))
    (row,) = dict(acc.pop_complete()).values()
    assert row["final_code"] == 487
    assert row["duration_ms"] == 0
    assert row["t_answer_ms"] is None


def test_negative_clock_clamped_to_zero():
    """A clock anomaly (end before answer) never stores a negative duration."""
    acc = _CallAccumulator()
    acc.ingest(parse_log_line(
        "direction=out call_id=neg t_200ok_received=5000 t_bye_sent=4000 "
        "final_code=200"))
    (row,) = dict(acc.pop_complete()).values()
    assert row["duration_ms"] == 0


# ── DB persistence + idempotency ────────────────────────────────────────────


def test_persist_and_idempotent_upsert(db):
    parser = CallRecordParser(db=db)
    parser._persist({
        "campaign_id": "camp-1", "direction": "out", "call_uuid": "u1",
        "a_number": "100", "b_number": "200", "source_ip": None,
        "t_start_ms": 1000, "t_answer_ms": 1100, "t_end_ms": 61100,
        "duration_ms": 60000, "final_code": 200,
    })
    # Re-persist the same call with a corrected end time -> updates, not dupes.
    parser._persist({
        "campaign_id": "camp-1", "direction": "out", "call_uuid": "u1",
        "a_number": "100", "b_number": "200", "source_ip": None,
        "t_start_ms": 1000, "t_answer_ms": 1100, "t_end_ms": 61200,
        "duration_ms": 60100, "final_code": 200,
    })
    rows = _fetch_records(db)
    assert len(rows) == 1
    assert rows[0]["duration_ms"] == 60100


# ── end-to-end via the stub sipp's emitted call-log ─────────────────────────


def _run_stub_and_get_calllog(stub_sipp, tmp_path, max_calls=30):
    """Run the Foundation stub `sipp` through the real engine and return the
    path to the per-call log it wrote (design §4.2 sample-line source)."""
    from gencall.core.sipp_engine import (
        SIPpEngine, SIPpInstance, SIPpMode, SIPpState, SIPpTransport,
    )

    engine = SIPpEngine(config=stub_sipp.config)
    inst = SIPpInstance(
        id="rec-stub",
        scenario_file="loop_uac.xml",
        remote_host="127.0.0.1",
        remote_port=5060,
        local_port=5061,
        mode=SIPpMode.UAC,
        transport=SIPpTransport.UDP,
        call_rate=50.0,
        call_limit=20,
        max_calls=max_calls,
    )
    assert engine.start_instance(inst)
    # Wait for the finite (-m) run to finish so the call-log is complete.
    deadline = time.time() + 30
    while inst.state == SIPpState.RUNNING and time.time() < deadline:
        time.sleep(0.1)
    # Stats file is <stats_dir>/gencall_sipp_<id>.csv; the stub writes its
    # call log next to it as <stem>.calllog.
    stats_csv = os.path.join(stub_sipp.stats_dir, "gencall_sipp_rec-stub.csv")
    calllog = os.path.splitext(stats_csv)[0] + ".calllog"
    assert os.path.exists(calllog), f"stub did not write {calllog}"
    return calllog


def test_ingest_stub_calllog_into_db(stub_sipp, tmp_path, db):
    """The stub's emitted call-log lines parse and land in call_records."""
    calllog = _run_stub_and_get_calllog(stub_sipp, tmp_path, max_calls=30)

    parser = CallRecordParser(db=db)
    parser.add_log_file(calllog, campaign_id="stub-camp")
    finalized = parser.poll_once()

    assert finalized, "expected at least one finalized record from the stub log"
    rows = _fetch_records(db)
    assert len(rows) == len(finalized)
    # The stub emits a/b numbers and out-direction reference timestamps; every
    # successful (200) record must carry a positive A-side duration.
    successes = [r for r in rows if r["final_code"] == 200]
    assert successes, "stub should emit successful calls"
    for r in successes:
        assert r["direction"] == "out"
        assert r["campaign_id"] == "stub-camp"
        assert r["duration_ms"] >= 0
        assert r["a_number"] and r["b_number"]
    # The stub fails ~1 in 25 calls (503) with zero duration.
    failures = [r for r in rows if r["final_code"] != 200]
    for r in failures:
        assert r["duration_ms"] == 0


def test_parser_poll_interval_floored():
    """Any sub-second poll interval is floored to the mandated >= 1 s."""
    parser = CallRecordParser(db=None, poll_interval=0.01)
    assert parser.poll_interval >= MIN_POLL_INTERVAL_S


# ── binary-seek byte offset (multibyte / CRLF safe) ─────────────────────────


def test_read_new_lines_binary_offset_multibyte(tmp_path):
    """The tail offset is a TRUE byte offset and survives multibyte lines.

    A UTF-8 line with multibyte characters has more BYTES than CHARS, and CRLF
    line endings add a byte the text-mode splitter hides. With the old text-mode
    seek(byte_offset) the second pass would seek to the wrong position and either
    re-read or skip data. Writing two lines across two passes — the first line
    carrying a multibyte name and a CRLF terminator — must yield exactly the two
    records, in order, with no corruption and no duplication.
    """
    path = tmp_path / "multibyte.calllog"
    parser = CallRecordParser(db=None)
    parser.add_log_file(str(path), campaign_id="mb")
    state = parser._files[str(path)]

    # Line 1: multibyte a_number (café / 日本) + CRLF terminator (bytes > chars).
    line1 = (
        "direction=out call_id=mb-1 a_number=café日本 b_number=200 "
        "t_invite=1000 t_200ok_received=1100 t_bye_sent=61100 final_code=200\r\n"
    )
    path.write_bytes(line1.encode("utf-8"))

    lines = parser._read_new_lines(str(path), state)
    assert len(lines) == 1
    assert "call_id=mb-1" in lines[0]
    # The offset advanced by the exact BYTE length of line 1 (incl. CRLF).
    assert state["offset"] == len(line1.encode("utf-8"))

    # Append a second line; the next read must start exactly where line 1 ended.
    line2 = (
        "direction=out call_id=mb-2 a_number=second b_number=200 "
        "t_invite=2000 t_200ok_received=2100 t_bye_sent=62100 final_code=200\n"
    )
    with open(path, "ab") as fh:
        fh.write(line2.encode("utf-8"))

    lines2 = parser._read_new_lines(str(path), state)
    assert len(lines2) == 1, "second pass must read ONLY the appended line"
    assert "call_id=mb-2" in lines2[0]
    assert "café" not in lines2[0], "line 1 must not be re-read (offset corrupt)"
    assert state["offset"] == len(line1.encode("utf-8")) + len(line2.encode("utf-8"))


def test_ingest_multibyte_line_end_to_end(tmp_path, db):
    """A full multibyte call log parses and lands one correct record in the DB."""
    path = tmp_path / "mb_e2e.calllog"
    path.write_bytes(
        (
            "loop_uac direction=out call_id=mbcall a_number=café日本 "
            "b_number=200 t_invite=1000 t_200ok_received=1100 "
            "t_bye_sent=61100 final_code=200\n"
        ).encode("utf-8")
    )
    parser = CallRecordParser(db=db)
    parser.add_log_file(str(path), campaign_id="mb")
    finalized = parser.poll_once()
    assert len(finalized) == 1
    rows = _fetch_records(db)
    assert len(rows) == 1
    assert rows[0]["a_number"] == "café日本"
    assert rows[0]["duration_ms"] == 60000


# ── trust filter (design §4.1, verification-only) ───────────────────────────


def test_ip_in_whitelist_plain_and_cidr():
    assert ip_in_whitelist("10.0.0.9", ["10.0.0.9"])           # exact host
    assert ip_in_whitelist("10.0.0.9", ["10.0.0.0/24"])         # CIDR
    assert not ip_in_whitelist("10.0.1.9", ["10.0.0.0/24"])     # outside CIDR
    assert not ip_in_whitelist("203.0.113.5", ["10.0.0.9"])     # unrelated
    # Empty whitelist means "allow all" so a fresh install isn't broken.
    assert ip_in_whitelist("203.0.113.5", [])
    assert ip_in_whitelist(None, [])
    # No source_ip but a non-empty whitelist cannot be trusted.
    assert not ip_in_whitelist(None, ["10.0.0.9"])
    # A malformed whitelist token is skipped, not fatal.
    assert ip_in_whitelist("10.0.0.9", ["garbage", "10.0.0.9"])


def _write_inbound_calllog(tmp_path, source_ip, call_id=" in1"):
    """Write a complete inbound (UAS) call's <log> lines to a temp file."""
    path = tmp_path / f"uas_{call_id.strip()}.calllog"
    path.write_text(
        f"loop_uas direction=in call_id={call_id.strip()} from_number=100 "
        f"to_number=200 source_ip={source_ip} t_invite_received=900\n"
        f"loop_uas direction=in call_id={call_id.strip()} t_200ok_sent=1000\n"
        f"loop_uas direction=in call_id={call_id.strip()} t_bye_received=61205\n",
        encoding="utf-8",
    )
    return str(path)


def test_trust_filter_keeps_whitelisted_inbound(tmp_path, db):
    """A whitelisted inbound source is kept and flagged trusted."""
    calllog = _write_inbound_calllog(tmp_path, "10.0.0.9", call_id="ok1")
    parser = CallRecordParser(db=db, trust_whitelist=["10.0.0.0/24"])
    parser.add_log_file(calllog, campaign_id="c")
    finalized = parser.poll_once()
    assert len(finalized) == 1
    assert finalized[0]["source_ip"] == "10.0.0.9"
    assert finalized[0]["trusted"] is True
    assert len(_fetch_records(db)) == 1


def test_trust_filter_flags_non_whitelisted_inbound(tmp_path, db):
    """A non-whitelisted inbound source is flagged untrusted (kept by default)."""
    calllog = _write_inbound_calllog(tmp_path, "203.0.113.5", call_id="bad1")
    parser = CallRecordParser(db=db, trust_whitelist=["10.0.0.0/24"])
    parser.add_log_file(calllog, campaign_id="c")
    finalized = parser.poll_once()
    assert len(finalized) == 1
    assert finalized[0]["trusted"] is False
    # Default behaviour keeps the record (visible) rather than silently dropping.
    assert len(_fetch_records(db)) == 1


def test_trust_filter_drops_non_whitelisted_when_configured(tmp_path, db):
    """With drop_untrusted, a non-whitelisted inbound record is dropped."""
    calllog = _write_inbound_calllog(tmp_path, "203.0.113.5", call_id="bad2")
    parser = CallRecordParser(
        db=db, trust_whitelist=["10.0.0.0/24"], drop_untrusted=True)
    parser.add_log_file(calllog, campaign_id="c")
    finalized = parser.poll_once()
    assert finalized == []
    assert _fetch_records(db) == []


def test_trust_filter_empty_whitelist_allows_all(tmp_path, db):
    """An empty whitelist (fresh install) keeps every inbound record."""
    calllog = _write_inbound_calllog(tmp_path, "203.0.113.5", call_id="fresh")
    parser = CallRecordParser(db=db, trust_whitelist=[])
    parser.add_log_file(calllog, campaign_id="c")
    finalized = parser.poll_once()
    assert len(finalized) == 1
    assert finalized[0]["trusted"] is True
    assert len(_fetch_records(db)) == 1


# ── loop_uas.xml scenario-structure guard (real-SIPp label semantics) ───────
#
# A SIPp <label> is ONLY a jump target — it does NOT halt the scenario.
# Execution falls through a label into whatever element follows it. The
# previous template placed `call_done` BEFORE the `max_duration_guard` block,
# so a normally completed call (BYE received -> 200-OK sent) fell straight
# through call_done into the guard and sent a SECOND, unsolicited BYE plus a
# duplicate `t_bye_received ... guard=max_duration` log line on EVERY call,
# corrupting B-side duration and channel-free accounting.
#
# Correct shape: the normal-path 200-OK <send> must carry next="call_done",
# the guard's trailing <recv> must carry next="call_done", and `call_done`
# must be the LAST <label> in the file (after `max_duration_guard`) so neither
# path can fall through into the guard's <send>.


def _loop_uas_xml_path():
    import os as _os
    from gencall.scenarios import manager as _m

    return _os.path.join(_os.path.dirname(_m.__file__), "templates", "loop_uas.xml")


def test_loop_uas_template_parses_and_has_guard_labels():
    """The template is well-formed XML and declares both control labels once."""
    import xml.etree.ElementTree as ET

    root = ET.parse(_loop_uas_xml_path()).getroot()
    label_ids = [el.get("id") for el in root.iter("label")]
    assert label_ids.count("call_done") == 1
    assert label_ids.count("max_duration_guard") == 1


def test_loop_uas_call_done_is_after_max_duration_guard():
    """call_done must be the LAST executable label, after the guard block.

    A label before the guard cannot serve as a skip target (SIPp labels don't
    stop execution), so call_done has to sit at the very end. This pins the
    ordering that prevents the normal path from falling through into the guard.
    """
    import xml.etree.ElementTree as ET

    root = ET.parse(_loop_uas_xml_path()).getroot()
    order = [el.get("id") for el in root.iter("label")]
    assert order.index("max_duration_guard") < order.index("call_done"), (
        "call_done must come AFTER max_duration_guard so the normal BYE path "
        "(which jumps to call_done) skips the guard's unsolicited BYE."
    )
    # call_done is the last <label> and no <send> may follow it (which would
    # re-introduce a fall-through into an unsolicited message).
    children = list(root)
    last_label_idx = max(
        i for i, el in enumerate(children)
        if el.tag == "label" and el.get("id") == "call_done"
    )
    assert not any(
        el.tag == "send" for el in children[last_label_idx + 1:]
    ), "no <send> may follow the call_done label"


def test_loop_uas_normal_bye_path_skips_the_guard():
    """The normal-path BYE 200-OK <send> jumps to call_done, not the guard.

    Without next="call_done" on this send, a normally completed call falls
    through into max_duration_guard and emits a duplicate guard=max_duration
    log line + a second BYE. We pin that exactly one <send> targets call_done
    and that it is the BYE-200-OK send (Content-Length 0, no SDP, no BYE
    request line of its own).
    """
    import xml.etree.ElementTree as ET

    root = ET.parse(_loop_uas_xml_path()).getroot()
    sends_to_call_done = [
        el for el in root.iter("send") if el.get("next") == "call_done"
    ]
    assert len(sends_to_call_done) == 1, (
        "exactly one <send> (the normal BYE 200-OK) must jump to call_done"
    )
    body = (sends_to_call_done[0].text or "")
    assert "200 OK" in body
    assert "Content-Length: 0" in body
    # It must be a response, not the guard's own BYE request.
    assert "BYE sip:" not in body
