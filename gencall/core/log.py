"""
GenCall logging setup.
"""

import logging
import logging.handlers
import os
import sys

from gencall.core.config import Config


def setup_logging(config: Config = None):
    if config is None:
        config = Config()

    root_logger = logging.getLogger("gencall")
    root_logger.setLevel(config.log_level)

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

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
