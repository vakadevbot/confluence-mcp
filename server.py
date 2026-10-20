"""Read-only Confluence MCP server for local stdio clients (OpenCode, etc.)."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from confluence_client import ConfluenceReader, Settings

mcp = FastMCP('confluence-readonly')


def _reader() -> ConfluenceReader:
    return ConfluenceReader(Settings.from_env())


@mcp.tool()
def get_page_by_id(page_id: str) -> dict[str, Any]:
    """Read a Confluence page by numeric ID. Returns rendered HTML, text, links,
    image URLs, embedded media and macro metadata. Read-only: no page changes.
    """
    return _reader().by_id(page_id)


@mcp.tool()
def get_page_by_url(url: str) -> dict[str, Any]:
    """Read a Confluence page by its full URL. URL must be within
    CONFLUENCE_BASE_URL. Supports /display/ links, viewpage.action?pageId=
    and /pages/{id}/ links. Returns HTML, text and media references.
    """
    return _reader().by_url(url)


if __name__ == '__main__':
    mcp.run(transport='stdio')
