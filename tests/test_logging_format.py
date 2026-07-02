"""
[logging] format=json — one JSON object per line with extra= fields.
"""

import json
import logging

from gencall.core.config import Config
from gencall.core.log import JsonFormatter, setup_logging


def test_json_formatter_fields_and_extras():
    fmt = JsonFormatter()
    logger = logging.getLogger("gencall.test.json")
    rec = logger.makeRecord(
        "gencall.test.json", logging.WARNING, "f.py", 1,
        "loop %s degraded", ("loop-1",), None,
        extra={"campaign_id": "loop-1", "node_id": 4},
    )
    out = json.loads(fmt.format(rec))
    assert out["level"] == "WARNING"
    assert out["logger"] == "gencall.test.json"
    assert out["message"] == "loop loop-1 degraded"
    assert out["campaign_id"] == "loop-1"
    assert out["node_id"] == 4
    assert out["ts"].endswith("+00:00")


def test_json_formatter_exception_field():
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        rec = logging.LogRecord("gencall.t", logging.ERROR, "f.py", 1,
                                "failed", (), sys.exc_info())
    out = json.loads(fmt.format(rec))
    assert "ValueError: boom" in out["exc"]


def test_setup_logging_is_idempotent_and_honors_format(tmp_path):
    cfg_file = tmp_path / "gencall.cfg"
    cfg_file.write_text(
        "[logging]\nlevel = 20\nformat = json\n"
        f"file = {tmp_path}/logs/g.log\n"
    )
    Config.reset()
    try:
        cfg = Config(str(cfg_file))
        logger = setup_logging(cfg)
        n_first = len(logger.handlers)
        logger = setup_logging(cfg)  # second call must not stack handlers
        assert len(logger.handlers) == n_first
        assert all(isinstance(h.formatter, JsonFormatter)
                   for h in logger.handlers)
    finally:
        Config.reset()
