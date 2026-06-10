# test_gencall.py is a standalone, self-running test script (it executes its
# whole suite at import time and calls sys.exit()), not a pytest module. Running
# it under pytest collection raises SystemExit and aborts the whole run, so we
# exclude it here. CI runs it directly via `python tests/test_gencall.py`.
collect_ignore = ["test_gencall.py"]

import os
import stat
import sys
import textwrap

import pytest


# Absolute path to the cross-platform fake `sipp` (tests/stubs/fake_sipp.py).
STUB_SIPP = os.path.join(os.path.dirname(__file__), "stubs", "fake_sipp.py")


def _make_sipp_launcher(bin_dir):
    """Create a directly-executable launcher that runs the fake_sipp stub.

    SIPpEngine.start_instance() builds `cmd = [config.sipp_command, ...]` and
    Popen's it directly, so config.sipp_command must point at something the OS
    can exec on its own. A bare `.py` isn't executable that way on Windows (no
    interpreter association under Popen) and may not be on POSIX without +x, so
    we wrap the stub in a tiny native launcher that forwards all args:

      * Windows: a `.cmd` batch file calling the current Python on the stub.
      * POSIX:   a shebang'd shell script, chmod +x.

    Returns the absolute launcher path to assign to config.sipp_command.
    """
    py = sys.executable
    if os.name == "nt":
        launcher = os.path.join(bin_dir, "fake_sipp.cmd")
        # %* forwards every argument; @echo off keeps stdout clean.
        with open(launcher, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write("@echo off\r\n")
            fh.write(f'"{py}" "{STUB_SIPP}" %*\r\n')
    else:
        launcher = os.path.join(bin_dir, "fake_sipp")
        with open(launcher, "w", encoding="utf-8") as fh:
            fh.write(
                textwrap.dedent(
                    f"""\
                    #!/bin/sh
                    exec "{py}" "{STUB_SIPP}" "$@"
                    """
                )
            )
        st = os.stat(launcher)
        os.chmod(launcher, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return launcher


@pytest.fixture
def stub_sipp(tmp_path, monkeypatch):
    """Point GenCall's config at the fake `sipp` with temp stats/log dirs.

    Resets the Config singleton (it's a process-wide singleton — see
    Config.reset()), writes a throwaway gencall.cfg whose [sipp] command is the
    stub launcher and whose stats_dir is a temp dir, and selects it via the
    GENCALL_CONFIG env var so Config picks it up with no global state bleed.

    Yields a small namespace with:
      * config       — the fresh Config instance (sipp_command -> stub)
      * stats_dir    — temp dir where the stub writes <id>.csv stats + .calllog
      * launcher     — the executable launcher path
      * config_path  — the temp gencall.cfg path
    """
    from gencall.core.config import Config

    bin_dir = tmp_path / "bin"
    stats_dir = tmp_path / "stats"
    bin_dir.mkdir()
    stats_dir.mkdir()

    launcher = _make_sipp_launcher(str(bin_dir))

    # A minimal config file pointing [sipp] at the stub. configparser needs no
    # escaping of backslashes in values, so Windows paths are fine verbatim.
    cfg_path = tmp_path / "gencall.cfg"
    cfg_path.write_text(
        textwrap.dedent(
            f"""\
            [sipp]
            command = {launcher}
            stats_dir = {stats_dir}
            open_file_limit = 256
            default_transport = udp

            [stats]
            interval = 1

            [database]
            engine = sqlite
            """
        ),
        encoding="utf-8",
    )

    # Reset + repoint the singleton at our temp config, restore afterwards.
    Config.reset()
    monkeypatch.setenv("GENCALL_CONFIG", str(cfg_path))
    config = Config(path=str(cfg_path))

    class StubEnv:
        pass

    env = StubEnv()
    env.config = config
    env.stats_dir = str(stats_dir)
    env.launcher = launcher
    env.config_path = str(cfg_path)

    yield env

    Config.reset()
