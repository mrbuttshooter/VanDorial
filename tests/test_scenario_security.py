"""Security regression: scenario names must not allow path traversal.

A scenario name maps onto ``<custom_dir>/<name>.xml``; without sanitization an
authenticated API caller could read, overwrite, or delete arbitrary .xml files
(e.g. the loop_uac.xml template that drives the call flow) via a name like
``../../../path``. These lock the fix in gencall.scenarios.manager.
"""

import os

import pytest

from gencall.scenarios.manager import ScenarioManager, _is_safe_name


def test_is_safe_name_rejects_traversal_and_separators():
    for bad in ("../evil", "../../etc/passwd", "a/b", "a\\b", "..",
                "/abs", ".", ".hidden", "", "name with space", "x" * 200):
        assert not _is_safe_name(bad), bad
    for good in ("loop_uac", "my_flow", "scenario-1", "a.b", "X1"):
        assert _is_safe_name(good), good


def test_save_rejects_traversal(tmp_path):
    m = ScenarioManager(custom_dir=str(tmp_path / "custom"))
    with pytest.raises(ValueError):
        m.save_custom_scenario("../../pwned", "<scenario/>")
    # nothing was written outside the custom dir
    assert not (tmp_path / "pwned.xml").exists()


def test_save_then_load_safe_name_works(tmp_path):
    m = ScenarioManager(custom_dir=str(tmp_path / "custom"))
    m.save_custom_scenario("good_one", "<scenario name='x'/>")
    assert m.get_scenario_content("good_one") == "<scenario name='x'/>"


def test_get_path_rejects_traversal(tmp_path):
    # Plant a file OUTSIDE the custom dir; a traversal name must not resolve to it.
    outside = tmp_path / "secret.xml"
    outside.write_text("top secret")
    custom = tmp_path / "custom"
    custom.mkdir()
    m = ScenarioManager(custom_dir=str(custom))
    assert m.get_scenario_path("../secret") is None
    assert m.get_scenario_content("../secret") is None


def test_delete_rejects_traversal(tmp_path):
    victim = tmp_path / "victim.xml"
    victim.write_text("do not delete")
    custom = tmp_path / "custom"
    custom.mkdir()
    m = ScenarioManager(custom_dir=str(custom))
    assert m.delete_custom_scenario("../victim") is False
    assert victim.exists()  # traversal delete was refused


def test_builtin_lookup_still_works(tmp_path):
    # A real builtin (exact dict key) still resolves — the guard only gates the
    # custom-dir path join.
    m = ScenarioManager(custom_dir=str(tmp_path / "custom"))
    assert m.get_scenario_path("loop_uac") is not None


# ── RCE: SIPp <exec command=> in a saved scenario (run later by the test runner) ──


def test_save_rejects_shell_exec_scenario(tmp_path):
    """A custom scenario with <exec command=...> is RCE once run via SIPp — reject."""
    from gencall.scenarios.manager import reject_dangerous_scenario
    m = ScenarioManager(custom_dir=str(tmp_path / "custom"))
    evil = '<scenario><send/><nop><action>' \
           '<exec command="touch /tmp/pwned"/></action></nop></scenario>'
    with pytest.raises(ValueError):
        m.save_custom_scenario("evil", evil)
    with pytest.raises(ValueError):
        reject_dangerous_scenario('<exec int_cmd="quit"/>')


def test_save_allows_benign_media_exec(tmp_path):
    """The media exec forms (rtp_stream/play_pcap_audio) stay allowed."""
    m = ScenarioManager(custom_dir=str(tmp_path / "custom"))
    ok = '<scenario><nop><action>' \
         '<exec rtp_stream="pause.pcap,loop"/></action></nop></scenario>'
    m.save_custom_scenario("media_ok", ok)
    assert m.get_scenario_content("media_ok") == ok


def test_reject_unsafe_worker_url_blocks_metadata_and_linklocal():
    from gencall.api.routes import _reject_unsafe_worker_url
    from fastapi import HTTPException
    # link-local (incl. cloud metadata 169.254.169.254), unspecified, multicast
    for bad in ("http://169.254.169.254/", "http://0.0.0.0:8080",
                "http://224.0.0.1:8080"):
        with pytest.raises(HTTPException):
            _reject_unsafe_worker_url(bad)
    # real VLAN worker, loopback (legit self/single-box proxy), hostname — allowed
    _reject_unsafe_worker_url("http://10.35.21.8:8080")
    _reject_unsafe_worker_url("http://127.0.0.1:8080")
    _reject_unsafe_worker_url("http://worker-3.lan:8080")
