"""File-based logging for the local MCP stdio server.

Do not write application log messages to stdout: MCP uses it for JSON-RPC.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_dir: Path | None = None) -> logging.Logger:
    """Write UTF-8 logs next to server.py, irrespective of the current directory.

    Use a distinct file per process so simultaneous OpenCode restarts cannot
    contend for the same rotating file on Windows.
    """
    directory = log_dir or Path(__file__).resolve().parent / "logs"
    directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("confluence_mcp")
    level_name = os.getenv("CONFLUENCE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        level = logging.INFO
    logger.setLevel(level)
    logger.propagate = False

    # Avoid duplicate handlers if this module is initialized more than once.
    logfile = directory / f"confluence-mcp-{os.getpid()}.log"
    if not any(isinstance(handler, RotatingFileHandler)
               and Path(handler.baseFilename) == logfile
               for handler in logger.handlers):
        handler = RotatingFileHandler(
            logfile,
            maxBytes=2 * 1024 * 1024,
            backupCount=2,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(process)d | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(handler)

    return logger
