"""Small, read-only Confluence page loader (Server/Data Center; Cloud v1 REST API)."""

from __future__ import annotations

import os
import re
import json
import time
from threading import Lock
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Resolve .env relative to this file, not to the MCP client's working directory.
load_dotenv(dotenv_path=Path(__file__).resolve().with_name('.env'))

# Shared by all readers/tools in this process, including concurrent calls.
_request_lock = Lock()
_last_request_at: float | None = None


def _wait_for_request(interval_ms: int) -> None:
    global _last_request_at
    with _request_lock:
        if _last_request_at is not None:
            remaining = interval_ms / 1000 - (time.monotonic() - _last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        _last_request_at = time.monotonic()


class ConfluenceError(Exception):
    """A readable error safe to return to an MCP client."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    username: str
    password: str
    timeout_seconds: float = 30.0
    max_page_bytes: int = 5_000_000
    ca_bundle: str | None = None
    request_interval_ms: int = 1000

    def __post_init__(self) -> None:
        if type(self.request_interval_ms) is not int or self.request_interval_ms < 0:
            raise ConfluenceError('CONFLUENCE_REQUEST_INTERVAL_MS must be a non-negative integer.')

    @classmethod
    def from_env(cls) -> Settings:
        base_url = os.getenv('CONFLUENCE_BASE_URL', '').strip().rstrip('/')
        username = os.getenv('CONFLUENCE_USERNAME', '').strip()
        password = os.getenv('CONFLUENCE_PASSWORD', '')
        if not base_url or not username or not password:
            raise ConfluenceError(
                'Set CONFLUENCE_BASE_URL, CONFLUENCE_USERNAME and '
                'CONFLUENCE_PASSWORD in the .env file next to server.py.'
            )
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ConfluenceError('CONFLUENCE_BASE_URL must be an http(s) URL without credentials, query or fragment.')
        try:
            timeout = float(os.getenv('CONFLUENCE_TIMEOUT_SECONDS', '30'))
            max_bytes = int(os.getenv('CONFLUENCE_MAX_PAGE_BYTES', '5000000'))
            interval_ms = int(os.getenv('CONFLUENCE_REQUEST_INTERVAL_MS', '1000'))
        except ValueError as exc:
            raise ConfluenceError('Invalid timeout, max page size or request interval in .env.') from exc
        if timeout <= 0 or max_bytes <= 0:
            raise ConfluenceError('Timeout and max page size must be positive.')
        ca_bundle = os.getenv('CONFLUENCE_CA_BUNDLE', '').strip() or None
        if ca_bundle and not Path(ca_bundle).is_file():
            raise ConfluenceError(f'CA certificate bundle does not exist: {ca_bundle}')
        return cls(base_url, username, password, timeout, max_bytes, ca_bundle, interval_ms)

    def validate_url(self, url: str) -> str:
        """Allow only URLs under the configured Confluence context path and origin."""
        supplied = urlsplit(url)
        base = urlsplit(self.base_url)
        if (supplied.scheme.lower() != base.scheme.lower()
                or supplied.netloc.lower() != base.netloc.lower()
                or supplied.username is not None or supplied.password is not None
                or not supplied.hostname):
            raise ConfluenceError('URL must point to the configured Confluence origin.')
        prefix = base.path.rstrip('/')
        if prefix and supplied.path != prefix and not supplied.path.startswith(prefix + '/'):
            raise ConfluenceError('URL is outside the configured Confluence context path.')
        return url


def page_id_from_url(url: str) -> str | None:
    """Recognize the common Confluence URL formats; a /display/ slug has no ID."""
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    for name in ('pageId', 'contentId'):
        for value in params.get(name, []):
            if re.fullmatch(r'[1-9]\d*', value):
                return value
    for pattern in (r'/pages/(\d+)(?:/|$)', r'/rest/api/content/(\d+)(?:/|$)'):
        match = re.search(pattern, parsed.path)
        if match and int(match.group(1)) > 0:
            return match.group(1)
    return None


def _absolute_link(url: str | None, page_url: str) -> str | None:
    if not url or url.lstrip().lower().startswith(('javascript:', 'data:')):
        return None
    parsed = urlsplit(url)
    if parsed.scheme and parsed.scheme not in ('http', 'https'):
        return None
    return urljoin(page_url, url)


def _readable_text(html: str) -> str:
    """Preserve punctuation and adjacency across inline HTML elements."""
    readable = BeautifulSoup(html, 'html.parser')
    for item in readable.find_all(['script', 'style', 'noscript', 'template']):
        item.decompose()
    for line_break in readable.find_all('br'):
        line_break.replace_with('\n')
    # Only structural boundaries introduce whitespace. Separating every text
    # node corrupts hosts such as test<span>-int-</span>api.example.org.
    for block in readable.find_all([
        'p', 'div', 'section', 'article', 'li', 'tr', 'pre',
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'ul', 'ol',
    ]):
        block.insert_before('\n')
        block.append('\n')
    for cell in readable.find_all(['td', 'th']):
        cell.append('\t')
    return readable.get_text().strip()


def parse_content(html: str, page_url: str) -> dict[str, Any]:
    """Preserve original HTML while separately extracting text and media references."""
    soup = BeautifulSoup(html, 'html.parser')
    images = []
    for image in soup.find_all('img'):
        src = image.get('src') or image.get('data-src') or image.get('data-image-src')
        images.append({'url': _absolute_link(src, page_url), 'alt': image.get('alt') or ''})

    embedded = []
    for element in soup.find_all(['iframe', 'object', 'embed', 'video', 'audio', 'source']):
        source = element.get('src') or element.get('data')
        if source:
            embedded.append({'type': element.name, 'url': _absolute_link(source, page_url)})

    links = []
    for anchor in soup.find_all('a', href=True):
        href = _absolute_link(anchor['href'], page_url)
        if href:
            links.append({'text': anchor.get_text().strip(), 'url': href})

    scripts = soup.find_all('script')
    # Never execute or include script/style content in the human-readable text.
    text = _readable_text(html)

    return {
        'html': html,
        'text': text,
        'images': images,
        'embedded_media': embedded,
        'links': links,
        'svg_count': len(soup.find_all('svg')),
        'canvas_count': len(soup.find_all('canvas')),
        'table_count': len(soup.find_all('table')),
        'script_count': len(scripts),
        'external_scripts': [link for tag in scripts if (link := _absolute_link(tag.get('src'), page_url))],
    }


def extract_macros(storage_html: str) -> list[dict[str, str]]:
    """Report macros that may not be fully represented by server-rendered HTML."""
    soup = BeautifulSoup(storage_html, 'html.parser')
    return [
        {'name': tag.get('ac:name', 'unknown'), 'id': tag.get('ac:macro-id', '')}
        for tag in soup.find_all(['ac:structured-macro', 'ac:macro'])
    ]


class ConfluenceReader:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.transport = transport  # Optional mock transport for offline tests.

    def _get(self, url: str, *, params: dict[str, str] | None = None, accept: str = 'text/html') -> tuple[bytes, str]:
        self.settings.validate_url(url)
        verify: bool | str = self.settings.ca_bundle or True
        try:
            with httpx.Client(
                auth=httpx.BasicAuth(self.settings.username, self.settings.password),
                timeout=self.settings.timeout_seconds,
                verify=verify,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                current = url
                for _ in range(6):
                    _wait_for_request(self.settings.request_interval_ms)
                    with client.stream('GET', current, params=params, headers={'Accept': accept}) as response:
                        params = None  # Redirect Location contains the destination query.
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get('location')
                            if not location:
                                raise ConfluenceError('Confluence redirected without a Location header.')
                            current = self.settings.validate_url(urljoin(str(response.url), location))
                            if 'login' in urlsplit(current).path.lower():
                                raise ConfluenceError('Confluence redirected to login. Check Basic Auth credentials.')
                            continue
                        if response.status_code == 401:
                            raise ConfluenceError('Confluence returned 401: check username/password or Basic Auth policy.')
                        if response.status_code == 403:
                            raise ConfluenceError('Confluence returned 403: this user cannot read the page.')
                        if response.status_code == 404:
                            raise ConfluenceError('Confluence returned 404: page not found (or access denied).')
                        if response.status_code >= 400:
                            raise ConfluenceError(f'Confluence returned HTTP {response.status_code}.')
                        content = bytearray()
                        for part in response.iter_bytes():
                            content.extend(part)
                            if len(content) > self.settings.max_page_bytes:
                                raise ConfluenceError('Page exceeds CONFLUENCE_MAX_PAGE_BYTES; increase it in .env.')
                        charset = response.charset_encoding or 'utf-8'
                        return bytes(content), charset
                raise ConfluenceError('Too many Confluence redirects.')
        except httpx.RequestError as exc:
            raise ConfluenceError(f'Cannot connect to Confluence ({type(exc).__name__}).') from exc

    def search_pages(
        self, query: str, search_in: str = 'all', limit: int = 5,
        start: int = 0, snippet_chars: int = 500,
    ) -> dict[str, Any]:
        """Search one bounded CQL page; never fetch full page bodies or follow next links."""
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ConfluenceError('query must contain 1–500 characters.')
        if search_in not in ('title', 'content', 'all'):
            raise ConfluenceError('search_in must be title, content or all.')
        for name, value, low, high in (
            ('limit', limit, 1, 20), ('start', start, 0, 1_000_000),
            ('snippet_chars', snippet_chars, 50, 1000),
        ):
            if type(value) is not int or not low <= value <= high:
                raise ConfluenceError(f'{name} must be an integer between {low} and {high}.')
        # Escape both the CQL string and Lucene text-search metacharacters.
        literal = re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r'\\\1', query.strip())
        phrase = '"' + literal + '"'
        value = '"' + phrase.replace('\\', '\\\\').replace('"', '\\"') + '"'
        clause = {'title': f'title ~ {value}', 'content': f'text ~ {value}',
                  'all': f'(title ~ {value} OR text ~ {value})'}[search_in]
        raw, charset = self._get(
            f'{self.settings.base_url}/rest/api/search',
            params={'cql': f'type = page AND {clause}', 'limit': str(limit),
                    'start': str(start), 'excerpt': 'highlight'},
            accept='application/json',
        )
        try:
            data = json.loads(raw.decode(charset))
        except (ValueError, UnicodeError, LookupError) as exc:
            raise ConfluenceError('Confluence search did not return valid JSON.') from exc
        if not isinstance(data, dict) or not isinstance(data.get('results'), list):
            raise ConfluenceError('Unexpected Confluence search response.')
        results = data['results']
        output: dict[str, Any] = {'pages': [], 'has_more': False, 'next_start': None,
                                  'truncated': False}
        for item in results[:limit]:
            if not isinstance(item, dict) or not isinstance(item.get('content'), dict):
                raise ConfluenceError('Unexpected Confluence search result.')
            content = item['content']
            page_id = str(content.get('id', ''))
            if not re.fullmatch(r'[1-9]\d*', page_id) or len(page_id) > 30:
                raise ConfluenceError('Invalid page ID in Confluence search result.')
            fallback = f'{self.settings.base_url}/pages/viewpage.action?pageId={page_id}'
            webui = (content.get('_links') or {}).get('webui') or item.get('url')
            url = urljoin(self.settings.base_url + '/', webui) if isinstance(webui, str) else fallback
            self.settings.validate_url(url)
            if len(json.dumps(url)) > 4000:
                url = fallback
            title = str(content.get('title') or item.get('title') or '')
            matches = _readable_text(str(item.get('excerpt') or ''))
            matches = matches.replace('@@@hl@@@', '').replace('@@@endhl@@@', '')
            matches = ' '.join(matches.split())
            entry = {'page_id': page_id, 'title': title[:300], 'url': url,
                     'matches': matches[:snippet_chars],
                     'truncated': len(title) > 300 or len(matches) > snippet_chars}
            output['pages'].append(entry)
            # Reserve room for pagination metadata. Includes JSON escaping overhead.
            if len(json.dumps(output)) > 15_500:
                output['pages'].pop()
                if not output['pages']:
                    raise ConfluenceError('Search result URL exceeds the response budget; check CONFLUENCE_BASE_URL.')
                output['truncated'] = True
                break
        consumed = len(output['pages'])
        links = data.get('_links') or {}
        output['has_more'] = bool(consumed < len(results) or links.get('next') or len(results) >= limit)
        output['next_start'] = start + consumed if output['has_more'] else None
        output['truncated'] = output['truncated'] or any(p['truncated'] for p in output['pages'])
        return output

    def by_id(self, page_id: str | int) -> dict[str, Any]:
        page_id = str(page_id)
        if not re.fullmatch(r'[1-9]\d*', page_id):
            raise ConfluenceError('page_id must be a positive decimal integer.')
        url = f'{self.settings.base_url}/rest/api/content/{page_id}'
        raw, charset = self._get(url, params={'expand': 'body.view,body.storage,version'}, accept='application/json')
        try:
            import json
            result = json.loads(raw.decode(charset))
        except (ValueError, UnicodeError, LookupError) as exc:
            raise ConfluenceError('Confluence REST API did not return valid JSON; check the URL and authentication.') from exc
        if not isinstance(result, dict):
            raise ConfluenceError('Unexpected Confluence REST API response.')
        if result.get('type') not in (None, 'page'):
            raise ConfluenceError('The specified ID does not refer to a Confluence page.')
        body = result.get('body') or {}
        view = (body.get('view') or {}).get('value')
        if not isinstance(view, str):
            raise ConfluenceError('body.view is unavailable: check Confluence REST API and page permissions.')
        links = result.get('_links') or {}
        canonical = urljoin(self.settings.base_url + '/', links.get('webui') or f'pages/viewpage.action?pageId={page_id}')
        # The API's canonical URL may be absolute, but never fetch it or use it to send credentials.
        parsed = parse_content(view, canonical)
        macros = extract_macros((body.get('storage') or {}).get('value') or '')
        return {
            'page_id': page_id,
            'title': result.get('title') or '',
            'url': canonical,
            'source': 'rest_body_view',
            'version': (result.get('version') or {}).get('number'),
            'macros': macros,
            **parsed,
            'note': 'HTML and media references are returned, but JavaScript is not executed; interactive macros, diagrams and images may require a browser or separate authenticated retrieval.',
        }

    def by_url(self, url: str) -> dict[str, Any]:
        self.settings.validate_url(url)  # Do this before extracting an ID.
        page_id = page_id_from_url(url)
        if page_id:
            return self.by_id(page_id)
        # /display/SPACE/Page+Title and other non-ID URLs: read the actual HTML page.
        raw, charset = self._get(url, accept='text/html')
        try:
            document = BeautifulSoup(raw.decode(charset, errors='replace'), 'html.parser')
        except LookupError as exc:
            raise ConfluenceError('Unsupported HTML character encoding.') from exc
        for selector in ('#main-content .wiki-content', '#main-content', '#content-body',
                         '#content .wiki-content', '.wiki-content', '[data-testid="page-content"]', 'article'):
            content = document.select_one(selector)
            if content and content.get_text(' ', strip=True):
                break
        else:
            raise ConfluenceError('Cannot locate the Confluence page content in HTML; the page may require JavaScript or login.')
        title_tag = document.select_one('meta[name="ajs-page-title"]')
        heading = document.select_one('#title-text, h1#title-text, h1')
        title = (title_tag.get('content') if title_tag else None) or (heading.get_text(' ', strip=True) if heading else '')
        meta_id = document.select_one('meta[name="ajs-page-id"]')
        possible_id = meta_id.get('content') if meta_id else None
        actual_id = possible_id if possible_id and re.fullmatch(r'[1-9]\d*', possible_id) else None
        return {
            'page_id': actual_id,
            'title': title,
            'url': url,
            'source': 'web_html',
            'version': None,
            'macros': [],
            **parse_content(str(content), url),
            'note': 'HTML and media references are returned, but JavaScript is not executed; interactive macros, diagrams and images may require a browser or separate authenticated retrieval.',
        }

