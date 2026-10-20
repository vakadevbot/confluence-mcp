"""Tests for local diagnostic logging; no Confluence server required."""

import os

from logging_config import configure_logging


def test_logging_to_file_and_traceback(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_LOG_LEVEL", "DEBUG")
    logger = configure_logging(tmp_path)
    try:
        logger.info("MCP startup")
        try:
            raise ValueError("Simulated startup problem")
        except ValueError:
            logger.exception("MCP failed")
        for handler in logger.handlers:
            handler.flush()

        logfile = tmp_path / f"confluence-mcp-{os.getpid()}.log"
        assert logfile.exists()
        data = logfile.read_text(encoding="utf-8")
        assert "MCP startup" in data
        assert "MCP failed" in data
        assert "Traceback (most recent call last)" in data
        assert "ValueError: Simulated startup problem" in data
    finally:
        # Do not leave a handler pointing at a pytest temporary directory.
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_logging_creates_missing_directory(tmp_path):
    target = tmp_path / "nested" / "logs"
    logger = configure_logging(target)
    try:
        assert target.is_dir()
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
