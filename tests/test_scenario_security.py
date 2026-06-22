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
