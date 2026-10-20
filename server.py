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

    # confluence_client loads .env; apply CONFLUENCE_LOG_LEVEL if set there.
    configure_logging()
    mcp = FastMCP("confluence-readonly")
except Exception:
    logger.exception("Failed to initialize MCP server")
    raise


def _reader() -> ConfluenceReader:
    return ConfluenceReader(Settings.from_env())


@mcp.tool()
def search_pages(query: str, search_in: str = 'all', limit: int = 5,
                 start: int = 0, snippet_chars: int = 500) -> dict[str, Any]:
    """Search Confluence pages by title/content using a literal word or phrase.
    search_in: title, content (Confluence text index), or all. Returns titles,
    URLs and short plain-text matching excerpts, preserving punctuation and inline
    text adjacency. Search-index excerpts may differ from page content: verify
    exact hostnames, URLs and identifiers with get_page_by_id before using them.
    No full page bodies. Defaults: 5 pages, 500 excerpt characters.
    Hard limits: 20 pages, 1000 excerpt characters, 16000 serialized JSON characters.
    Use next_start as start to explicitly request another page; no auto-pagination.
    truncated indicates shortened fields or a response-size limit.
    """
    logger.info('search_pages started: search_in=%s, limit=%s, start=%s', search_in, limit, start)
    try:
        result = _reader().search_pages(query, search_in, limit, start, snippet_chars)
    except Exception:
        logger.exception('search_pages failed')
        raise
    logger.info('search_pages completed: pages=%d, has_more=%s',
                len(result['pages']), result['has_more'])
    return result


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

