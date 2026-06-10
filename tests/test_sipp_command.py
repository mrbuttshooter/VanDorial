"""
SIPp call-path command-line tests (no real SIPp required).

These lock in the prod bugs the review confirmed in the loop runner, so a
regression that re-introduces them fails CI on a plain box:

  * NO ``-bg`` — SIPp must run in the FOREGROUND under our process group, never
    daemonized (a backgrounded SIPp forks and the parent exits, orphaning the
    real dialer).
  * RTP port window — ``-min_rtp_port`` / ``-max_rtp_port`` are ALWAYS emitted so
    media lands inside the firewalled range.
  * ``-i`` AND ``-mi`` when a local bind IP is set, so signalling and media share
    the SIP-facing interface (SDP matches the -rtp_echo socket).
  * stderr is discarded (DEVNULL) on launch — an unread stderr PIPE can deadlock
    a busy SIPp; SIPp's own -trace_err file carries the detail.
"""

from gencall.core.config import Config
from gencall.core.sipp_engine import (
    SIPpInstance,
    SIPpMode,
    SIPpState,
    SIPpTransport,
)


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


def test_build_command_always_emits_rtp_port_window(stub_sipp):
    """RTP media ports are pinned inside the firewalled config window."""
    inst = _instance()
    cmd = inst.build_command(stub_sipp.config)
    assert "-min_rtp_port" in cmd
    assert "-max_rtp_port" in cmd
    cfg = stub_sipp.config
    # The values immediately follow their flags and match config.
    assert cmd[cmd.index("-min_rtp_port") + 1] == str(cfg.min_rtp_port)
    assert cmd[cmd.index("-max_rtp_port") + 1] == str(cfg.max_rtp_port)


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
    assert "-min_rtp_port" in cmd  # still pinned


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
