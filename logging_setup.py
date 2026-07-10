"""
Full path: steeljet_tracker/logging_setup.py

Central logger setup so every module shares one configured logger (file +
console handlers) instead of each file wiring its own.
"""

import logging
from logging.handlers import RotatingFileHandler


def build_logger(config):
    logger = logging.getLogger("steeljet")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    file_handler = RotatingFileHandler(
        config.log_file,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console_handler)

    return logger
