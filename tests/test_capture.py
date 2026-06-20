import os
import sys
import time

from gencall.core.config import Config
from gencall.core.capture import build_capture_filter, CaptureManager


def test_capture_config_defaults(tmp_path, monkeypatch):
    cfg_path = tmp_path / "gencall.cfg"
    cfg_path.write_text("[sipp]\nstats_dir = %s\n" % (tmp_path / "stats"), encoding="utf-8")
    Config.reset()
    monkeypatch.setenv("GENCALL_CONFIG", str(cfg_path))
    c = Config(path=str(cfg_path))
    assert c.capture_command == "tcpdump"
    assert c.capture_max_seconds == 300
    assert c.capture_max_mb == 100
    assert c.capture_snaplen == 0
    # capture dir defaults to the sipp stats dir
    assert c.capture_dir == c.sipp_stats_dir
    Config.reset()


def test_build_capture_filter_scopes_to_dest_and_ports():
    f = build_capture_filter(dest_host="203.0.113.10", dest_port=5060,
                             local_port=5071, media_port=10000, transport="udp")
    assert "host 203.0.113.10" in f
    assert "port 5060" in f
    assert "port 5071" in f
    # RTP base + RTCP/echo neighbours
    assert "portrange 10000-10002" in f


def test_capture_manager_start_list_stop_delete(tmp_path, monkeypatch):
    # Cross-platform fake tcpdump: writes the -w file then sleeps until killed.
    stub = os.path.join(os.path.dirname(__file__), "stubs", "fake_tcpdump.py")
    mgr = CaptureManager(command=f'"{sys.executable}" "{stub}"', capture_dir=str(tmp_path),
                         max_seconds=0, max_mb=0)
    cap = mgr.start(campaign_id="loop-abc", bpf="udp", iface="any")
    assert cap["running"] is True
    listed = mgr.list("loop-abc")
    assert len(listed) == 1 and listed[0]["id"] == cap["id"]
    # the output file exists
    assert os.path.isfile(mgr.path(cap["id"]))
    mgr.stop(cap["id"])
    time.sleep(0.2)
    assert mgr.list("loop-abc")[0]["running"] is False
    mgr.delete(cap["id"])
    assert mgr.list("loop-abc") == []
