"""Read-only Confluence MCP server for local stdio clients (OpenCode, etc.)."""

from __future__ import annotations

import os
import sys
from typing import Any

# Initialize file logging before third-party imports. Import failures will be
# recorded even if the MCP process exits with code 1 during startup.
from logging_config import configure_logging

logger = configure_logging()
logger.info("Process starting: pid=%s, Python=%s", os.getpid(), sys.version.split()[0])

try:
    from mcp.server.fastmcp import FastMCP
    from confluence_client import ConfluenceReader, Settings
except Exception:
    logger.exception("Failed to import MCP server dependencies")
    raise

mcp = FastMCP("confluence-readonly")


def _reader() -> ConfluenceReader:
    return ConfluenceReader(Settings.from_env())


@mcp.tool()
def get_page_by_id(page_id: str) -> dict[str, Any]:
    """Read a Confluence page by numeric ID. Returns rendered HTML, text, links,
    image URLs, embedded media and macro metadata. Read-only: no page changes.
    """
    logger.info("get_page_by_id started: page_id=%r", page_id)
    try:
        result = _reader().by_id(page_id)
    except Exception:
        logger.exception("get_page_by_id failed: page_id=%r", page_id)
        raise
    logger.info(
        "get_page_by_id completed: page_id=%r, html_chars=%d, images=%d",
        result.get("page_id"), len(result.get("html", "")), len(result.get("images", [])),
    )
    return result


@mcp.tool()
def get_page_by_url(url: str) -> dict[str, Any]:
    """Read a Confluence page by its full URL. URL must be within
    CONFLUENCE_BASE_URL. Supports /display/ links, viewpage.action?pageId=
    and /pages/{id}/ links. Returns HTML, text and media references.
    """
    # Avoid logging full URLs: query strings may contain confidential tokens.
    logger.info("get_page_by_url started")
    try:
        result = _reader().by_url(url)
    except Exception:
        logger.exception("get_page_by_url failed")
        raise
    logger.info(
        "get_page_by_url completed: page_id=%r, source=%s, html_chars=%d, images=%d",
        result.get("page_id"), result.get("source"),
        len(result.get("html", "")), len(result.get("images", [])),
    )
    return result


if __name__ == "__main__":
    try:
        logger.info("Starting MCP stdio transport")
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        logger.info("MCP server interrupted")
    except BaseException:
        logger.exception("MCP server terminated unexpectedly")
        raise
    finally:
        logger.info("MCP server stopped")
