"""Application logging configuration."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from aambridge.settings import Settings

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def configure_logging(settings: Settings) -> None:
    """Configure console and rotating-file logging for command-line tools."""

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        settings.log_dir / "aambridge.log",
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(settings.log_level.upper())
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

