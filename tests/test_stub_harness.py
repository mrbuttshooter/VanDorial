"""
Foundation-stage tests for the fake-SIPp harness.

Proves the cross-platform stub `sipp` (tests/stubs/fake_sipp.py), driven through
the REAL SIPpEngine process-control code, behaves like enough of SIPp for every
later stage to be tested without real SIPp/Docker/Linux:

  * it starts via SIPpEngine.start_instance and reaches RUNNING,
  * it writes a SIPp-format stats CSV that SIPpEngine._read_stats() parses into
    non-zero TotalCallCreated / SuccessfulCall counters,
  * it writes per-call structured log lines (design §4.2 fields),
  * it stops cleanly via SIPpEngine.stop_instance with no orphaned process,
  * a finite (-m) run completes on its own and exits 0.

These run on Windows (the dev sandbox) and POSIX alike — see conftest's
stub_sipp fixture for how config.sipp_command is wired to the stub.
"""

import os
import time


from gencall.core.sipp_engine import (
    SIPpEngine,
    SIPpInstance,
    SIPpMode,
    SIPpState,
    SIPpTransport,
)


def _wait_until(predicate, timeout=15.0, interval=0.1):
    """Poll predicate() until truthy or timeout; return its last value."""
    deadline = time.time() + timeout
    val = predicate()
    while not val and time.time() < deadline:
        time.sleep(interval)
        val = predicate()
    return val


def _make_instance(stub_env, **overrides):
    """Build a SIPpInstance targeting the stub, with sane test defaults."""
    params = dict(
        id="harness-1",
        scenario_file="dummy.xml",
        remote_host="127.0.0.1",
        remote_port=5060,
        local_port=5061,
        mode=SIPpMode.UAC,
        transport=SIPpTransport.UDP,
        call_rate=20.0,
        call_limit=10,
        max_calls=0,
    )
    params.update(overrides)
    return SIPpInstance(**params)


def test_stub_starts_and_reaches_running(stub_sipp):
    """SIPpEngine.start_instance launches the stub and marks it RUNNING."""
    engine = SIPpEngine(stub_sipp.config)
    inst = _make_instance(stub_sipp)

    assert engine.start_instance(inst) is True
    try:
        assert inst.state == SIPpState.RUNNING
        assert inst.error_message == ""
        # The stub process is alive (no immediate exit / ENOENT).
        assert inst._process is not None
        assert inst._process.poll() is None
        # The command really pointed at our stub launcher.
        assert inst._stats_file.startswith(stub_sipp.stats_dir)
    finally:
        engine.stop_instance(inst.id)


def test_stub_produces_parseable_stats(stub_sipp):
    """The stub's stats CSV is parsed by the real engine into growing counters."""
    engine = SIPpEngine(stub_sipp.config)
    inst = _make_instance(stub_sipp, id="harness-stats", call_rate=30.0)

    assert engine.start_instance(inst) is True
    try:
        # The monitor thread reads stats every config.stats_interval (1 s here);
        # wait for the stub to have created some calls and the engine to ingest.
        got = _wait_until(lambda: inst.stats.total_calls > 0, timeout=15.0)
        assert got, "engine never parsed a non-zero TotalCallCreated from the stub"

        # The stats file exists, is SIPp-format (';'-separated, header + rows).
        assert os.path.exists(inst._stats_file)
        with open(inst._stats_file, encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        assert len(lines) >= 2
        header = lines[0].split(";")
        for col in (
            "TotalCallCreated",
            "SuccessfulCall(C)",
            "FailedCall(C)",
            "CurrentCall",
            "Retransmissions(C)",
        ):
            assert col in header, f"missing column {col} in stub stats header"

        # Engine-parsed counters are coherent.
        assert inst.stats.total_calls > 0
        assert inst.stats.successful_calls >= 0
        assert inst.stats.successful_calls <= inst.stats.total_calls
    finally:
        engine.stop_instance(inst.id)


def test_stub_writes_per_call_log(stub_sipp):
    """The stub emits per-call structured log lines with the §4.2 UAC fields."""
    engine = SIPpEngine(stub_sipp.config)
    inst = _make_instance(stub_sipp, id="harness-log", call_rate=30.0)

    assert engine.start_instance(inst) is True
    try:
        # Call log path is derived from the stats file: <stem>.calllog.
        stem, _ = os.path.splitext(inst._stats_file)
        call_log = stem + ".calllog"

        got = _wait_until(
            lambda: os.path.exists(call_log) and os.path.getsize(call_log) > 0,
            timeout=15.0,
        )
        assert got, "stub never wrote a per-call log"

        with open(call_log, encoding="utf-8") as fh:
            first = fh.readline()
        # Design §4.2 UAC fields must all be present on a line.
        for field in (
            "call_id=",
            "a_number=",
            "b_number=",
            "t_invite=",
            "t_200ok_received=",
            "t_bye_sent=",
            "final_code=",
        ):
            assert field in first, f"missing {field!r} in call log line: {first!r}"
    finally:
        engine.stop_instance(inst.id)


def test_stub_stops_without_orphan(stub_sipp):
    """stop_instance terminates the stub cleanly — no orphaned process left."""
    engine = SIPpEngine(stub_sipp.config)
    inst = _make_instance(stub_sipp, id="harness-stop", call_rate=20.0)

    assert engine.start_instance(inst) is True
    proc = inst._process
    assert proc is not None
    # Let it run briefly so there's something to stop.
    _wait_until(lambda: inst.stats.total_calls > 0, timeout=10.0)

    assert engine.stop_instance(inst.id) is True
    assert inst.state == SIPpState.STOPPED
    # The process really exited (poll() returns a code, not None).
    exited = _wait_until(lambda: proc.poll() is not None, timeout=10.0)
    assert exited, "stub process was not reaped after stop_instance (orphan)"


def test_stub_finite_run_completes(stub_sipp):
    """A bounded (-m) run finishes on its own and exits cleanly (code 0)."""
    engine = SIPpEngine(stub_sipp.config)
    # Small max_calls + high rate -> completes within a couple of seconds.
    inst = _make_instance(
        stub_sipp, id="harness-finite", call_rate=50.0, max_calls=20, call_limit=20
    )

    assert engine.start_instance(inst) is True
    proc = inst._process
    assert proc is not None

    exited = _wait_until(lambda: proc.poll() is not None, timeout=20.0)
    assert exited, "finite (-m) stub run did not complete on its own"
    assert proc.returncode == 0

    # The monitor should observe normal completion (STOPPED, not ERROR).
    settled = _wait_until(
        lambda: inst.state in (SIPpState.STOPPED, SIPpState.ERROR), timeout=5.0
    )
    assert settled
    assert inst.state == SIPpState.STOPPED, f"unexpected final state {inst.state}"
    assert inst.stats.total_calls > 0
