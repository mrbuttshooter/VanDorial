"""
GenCall logging setup.

Two output formats, selected with ``[logging] format``:

  text (default)  [2026-07-02 10:00:00] WARNING  gencall.loops: message
  json            {"ts":"2026-07-02T10:00:00+00:00","level":"WARNING",...}

The JSON formatter emits one object per line so journald/Loki/jq can query
fields instead of grepping. Anything passed via ``extra=`` at the call site
(e.g. ``logger.warning("...", extra={"campaign_id": cid})``) rides along as a
top-level field — the formatter picks up every non-standard record attribute,
so new context fields need no formatter change.
"""

import datetime
import json
import logging
import logging.handlers
import os
import sys

from gencall.core.config import Config

# Attributes every LogRecord carries; anything else was passed via extra= and
# belongs in the JSON output as a context field.
_STANDARD_RECORD_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None))
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, message (+ extras, exc)."""

    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.datetime.fromtimestamp(
                record.created, datetime.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_RECORD_ATTRS:
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def _build_formatter(config: Config) -> logging.Formatter:
    fmt = (config.get("logging", "format", "text") or "text").strip().lower()
    if fmt == "json":
        return JsonFormatter()
    return logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def setup_logging(config: Config = None):
    if config is None:
        config = Config()

    root_logger = logging.getLogger("gencall")
    root_logger.setLevel(config.log_level)

    # Idempotent: create_app()/CLI paths may call this more than once in a
    # process (tests especially); stacking handlers would duplicate every line.
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = _build_formatter(config)

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    # File handler (if log directory exists or can be created)
    log_file = config.log_file
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=config.getint("logging", "max_bytes", 10485760),
            backupCount=config.getint("logging", "backup_count", 5),
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    return root_logger
