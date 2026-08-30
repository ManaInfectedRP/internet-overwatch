"""Application logging — rotating file log plus console output."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from app.utils.platform import log_dir

_CONFIGURED = False
LOG_FILENAME = "app.log"

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def log_file_path() -> Path:
    return log_dir() / LOG_FILENAME


def setup_logging(level: str = "INFO", console: bool = True) -> logging.Logger:
    """Configure the root logger once. Safe to call repeatedly."""
    global _CONFIGURED
    root = logging.getLogger("overwatch")
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    root.setLevel(numeric)

    if _CONFIGURED:
        for handler in root.handlers:
            handler.setLevel(numeric)
        return root

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        path = log_file_path()
        file_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(numeric)
        root.addHandler(file_handler)
    except OSError:  # pragma: no cover - read-only filesystem
        pass

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        stream.setLevel(numeric)
        root.addHandler(stream)

    root.propagate = False
    _CONFIGURED = True
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced child logger."""
    if not _CONFIGURED:
        setup_logging()
    if name.startswith("overwatch"):
        return logging.getLogger(name)
    return logging.getLogger(f"overwatch.{name}")


def set_level(level: str) -> None:
    logger = logging.getLogger("overwatch")
    numeric = getattr(logging, str(level).upper(), logging.INFO)
    logger.setLevel(numeric)
    for handler in logger.handlers:
        handler.setLevel(numeric)
