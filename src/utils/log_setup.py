"""Shared logging configuration with file rotation."""

import logging
import os
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s -- %(message)s"
_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_BACKUP_COUNT = 5


def configure_logging(
    service_name: str,
    *,
    log_dir: str = "logs",
    level: int = logging.INFO,
) -> logging.Logger:
    """Set up root logger with stdout + rotating file handler."""
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)

    # Clear any existing handlers to avoid duplicates
    root.handlers.clear()

    formatter = logging.Formatter(_LOG_FORMAT)

    # Stdout handler
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    # Rotating file handler
    log_path = os.path.join(log_dir, f"{service_name}.log")
    file_handler = RotatingFileHandler(
        log_path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Suppress noisy libraries
    logging.getLogger("LiteLLM").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return root
