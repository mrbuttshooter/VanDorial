from gencall.core.config import Config


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
