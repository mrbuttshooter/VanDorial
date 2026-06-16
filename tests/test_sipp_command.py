"""
SIPp call-path command-line tests (no real SIPp required).

These lock in the prod bugs the review confirmed in the loop runner, so a
regression that re-introduces them fails CI on a plain box:

  * NO ``-bg`` — SIPp must run in the FOREGROUND under our process group, never
    daemonized (a backgrounded SIPp forks and the parent exits, orphaning the
    real dialer).
  * RTP echo port — ``-mp`` is ALWAYS emitted with a UNIQUE base port per instance
    so media lands inside the firewalled range and no two SIPps collide (exit 254).
    The engine standardizes on ``-mp`` (the only RTP-port flag on SIPp 3.6.1, and a
    valid alias for ``-min_rtp_port`` on 3.7.3), so the range flags never appear.
  * ``-i`` AND ``-mi`` when a local bind IP is set, so signalling and media share
    the SIP-facing interface (SDP matches the -rtp_echo socket).
  * stderr is discarded (DEVNULL) on launch — an unread stderr PIPE can deadlock
    a busy SIPp; SIPp's own -trace_err file carries the detail.
"""

import pytest

from gencall.core.config import Config
from gencall.core.sipp_engine import (
    SIPpInstance,
    SIPpMode,
    SIPpState,
    SIPpTransport,
)


class _WinCfg:
    """Minimal config exposing just the RTP window + file limit (allocator tests)."""

    def __init__(self, lo, hi):
        self.min_rtp_port = lo
        self.max_rtp_port = hi
        self.sipp_file_limit = 256


def _instance(**kw):
    base = dict(
        id="t1",
        scenario_file="/tmp/scn.xml",
        remote_host="1.2.3.4",
        remote_port=5060,
    )
    base.update(kw)
    return SIPpInstance(**base)


def test_build_command_has_no_bg_flag(stub_sipp):
    """A daemonizing ``-bg`` must never appear — SIPp runs in the foreground."""
    inst = _instance()
    cmd = inst.build_command(stub_sipp.config)
    assert "-bg" not in cmd


def test_build_command_pins_rtp_echo_port_with_mp(stub_sipp):
    """RTP echo media port uses SIPp's -mp flag, pinned inside the config window.
    The engine standardizes on -mp (the only RTP-port flag on 3.6.1, an alias for
    -min_rtp_port on 3.7.3), so the range flags must never be emitted."""
    inst = _instance()
    cmd = inst.build_command(stub_sipp.config)
    assert "-mp" in cmd
    assert "-min_rtp_port" not in cmd and "-max_rtp_port" not in cmd
    cfg = stub_sipp.config
    # media_port == 0 on the instance falls back to config.min_rtp_port.
    assert cmd[cmd.index("-mp") + 1] == str(cfg.min_rtp_port)


def test_build_command_emits_i_and_mi_when_local_ip_set(stub_sipp):
    """A local bind IP yields both -i (signalling) and -mi (media)."""
    inst = _instance(local_ip="10.0.0.5")
    cmd = inst.build_command(stub_sipp.config)
    assert "-i" in cmd and cmd[cmd.index("-i") + 1] == "10.0.0.5"
    assert "-mi" in cmd and cmd[cmd.index("-mi") + 1] == "10.0.0.5"


def test_build_command_omits_i_mi_when_no_local_ip(stub_sipp):
    """No local IP => no -i/-mi (SIPp binds all interfaces), but RTP window stays."""
    inst = _instance(local_ip="")
    cmd = inst.build_command(stub_sipp.config)
    assert "-i" not in cmd
    assert "-mi" not in cmd
    assert "-mp" in cmd  # RTP echo port still pinned


def test_fixed_duration_does_not_pass_d_flag_via_engine(stub_sipp):
    """Engine no longer relies on -d for the hold (it travels in the CSV)."""
    # build_command still emits -d only when instance.duration > 0; the LoopEngine
    # sets duration=0, so assert that path: a 0-duration instance has no -d.
    inst = _instance(duration=0)
    cmd = inst.build_command(stub_sipp.config)
    assert "-d" not in cmd


def test_start_instance_uses_devnull_stderr(stub_sipp, monkeypatch):
    """Popen is launched with stderr discarded (no unread PIPE to deadlock on)."""
    import subprocess

    from gencall.core.sipp_engine import SIPpEngine

    captured = {}
    real_popen = subprocess.Popen

    def spy_popen(cmd, **kwargs):
        captured["stderr"] = kwargs.get("stderr")
        captured["stdout"] = kwargs.get("stdout")
        return real_popen(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy_popen)
    engine = SIPpEngine(config=stub_sipp.config)
    inst = _instance(scenario_file="/tmp/scn.xml", max_calls=1)
    try:
        engine.start_instance(inst)
    finally:
        engine.stop_all()
    assert captured.get("stderr") == subprocess.DEVNULL
    assert captured.get("stdout") == subprocess.DEVNULL


def test_engine_assigns_unique_media_ports(stub_sipp):
    """Two instances with no explicit media_port get DISTINCT -mp base ports.

    Regression for the 254 crash: a one-shot test defaulted to the same RTP base
    port as the persistent UAS (both 16384/min_rtp_port), so the second SIPp hit
    'Address already in use' and exited 254. The engine now hands out unique base
    ports from the config window, stepped so RTP/RTCP/echo (+0/+1/+2) never overlap.
    """
    from gencall.core.sipp_engine import SIPpEngine

    engine = SIPpEngine(config=stub_sipp.config)
    a = _instance(id="a", scenario_file="/tmp/scn.xml", max_calls=0)
    b = _instance(id="b", scenario_file="/tmp/scn.xml", max_calls=0)
    try:
        assert engine.start_instance(a) is True
        assert engine.start_instance(b) is True
        assert a.media_port and b.media_port
        assert a.media_port != b.media_port
        assert b.media_port - a.media_port >= 4  # no +2 echo overlap
    finally:
        engine.stop_all()


def test_engine_releases_media_port_on_stop_for_reuse(stub_sipp):
    """Stopping an instance frees its base port so a later instance can reuse it."""
    from gencall.core.sipp_engine import SIPpEngine

    engine = SIPpEngine(config=stub_sipp.config)
    a = _instance(id="a", scenario_file="/tmp/scn.xml", max_calls=0)
    try:
        engine.start_instance(a)
        first = a.media_port
        assert first
        engine.stop_instance("a")
        assert first not in engine._media_ports_used  # released
        b = _instance(id="b", scenario_file="/tmp/scn.xml", max_calls=0)
        engine.start_instance(b)
        assert b.media_port == first  # lowest free port reused
    finally:
        engine.stop_all()


def test_engine_registers_explicit_media_port(stub_sipp):
    """An explicitly-set media_port is honored AND reserved (won't be reissued)."""
    from gencall.core.sipp_engine import SIPpEngine

    engine = SIPpEngine(config=stub_sipp.config)
    pinned = stub_sipp.config.min_rtp_port  # what a naive instance would grab
    a = _instance(id="a", scenario_file="/tmp/scn.xml", max_calls=0, media_port=pinned)
    b = _instance(id="b", scenario_file="/tmp/scn.xml", max_calls=0)
    try:
        engine.start_instance(a)
        engine.start_instance(b)
        assert a.media_port == pinned
        assert b.media_port != pinned  # allocator skipped the reserved port
    finally:
        engine.stop_all()


def test_double_start_of_same_id_is_refused(stub_sipp):
    """A second start of a RUNNING/STARTING id is refused (no double-launch).

    This guards the UAS restart race: start_instance sets STARTING synchronously
    under the lock and rejects a re-entry while RUNNING or STARTING, so two
    passes can never both spawn a process fighting for the same port.
    """
    import time

    from gencall.core.sipp_engine import SIPpEngine

    engine = SIPpEngine(config=stub_sipp.config)
    inst = _instance(scenario_file="/tmp/scn.xml", max_calls=0)  # long-lived
    try:
        assert engine.start_instance(inst) is True
        # Wait until it is RUNNING, then a re-start of the same id is refused.
        deadline = time.time() + 5
        while time.time() < deadline and inst.state != SIPpState.RUNNING:
            time.sleep(0.05)
        assert inst.state == SIPpState.RUNNING
        again = _instance(id="t1", scenario_file="/tmp/scn.xml", max_calls=0)
        assert engine.start_instance(again) is False
    finally:
        engine.stop_all()


def test_alloc_media_port_raises_when_window_exhausted():
    """When the RTP window is exhausted the allocator RAISES (a clean capacity
    error) instead of returning a colliding base port — the old behaviour handed
    back min_rtp_port, which the UAS already held, so the next SIPp exited 254."""
    from gencall.core.sipp_engine import SIPpEngine

    eng = SIPpEngine(config=_WinCfg(16384, 16394))  # exactly 3 base slots
    assert [eng._alloc_media_port() for _ in range(3)] == [16384, 16388, 16392]
    with pytest.raises(RuntimeError, match="exhausted"):
        eng._alloc_media_port()


def test_release_media_port_zeroes_and_frees():
    """Releasing a port frees it for reuse AND zeroes the instance's media_port,
    so a later release of the same stopped instance cannot discard a port that
    has since been re-allocated to another instance."""
    from gencall.core.sipp_engine import SIPpEngine

    eng = SIPpEngine(config=_WinCfg(16384, 16394))
    inst = _instance()
    inst.media_port = eng._alloc_media_port()
    assert inst.media_port == 16384
    eng._release_media_port(inst)
    assert inst.media_port == 0                  # zeroed -> re-release is a no-op
    assert 16384 not in eng._media_ports_used    # freed
    assert eng._alloc_media_port() == 16384       # reused
