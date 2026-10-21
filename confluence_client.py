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
from urllib.parse import parse_qs, quote, urljoin, urlsplit

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


def space_metadata(space: Any) -> dict[str, str | None] | None:
    if not isinstance(space, dict) or not space:
        return None
    return {key: str(space[key])[:size] if space.get(key) is not None else None
            for key, size in (('id', 30), ('key', 255), ('name', 300))}


def page_metadata(content: dict[str, Any]) -> dict[str, Any]:
    """Small, stable metadata for both Cloud and Server/Data Center responses."""
    def user(value: Any) -> dict[str, str | None] | None:
        if not isinstance(value, dict):
            return None
        name = value.get('displayName')
        identifier = value.get('accountId') or value.get('userKey') or value.get('username')
        if not name and not identifier:
            return None
        return {'display_name': str(name)[:128] if name else None,
                'id': str(identifier)[:64] if identifier else None}

    history = content.get('history') or {}
    version = content.get('version') or {}
    return {
        'space': space_metadata(content.get('space')),
        'created_by': user(history.get('createdBy')),
        'created_at': str(history['createdDate'])[:40] if history.get('createdDate') else None,
        'last_modified_by': user(version.get('by')),
        'last_modified_at': str(version['when'])[:40] if version.get('when') else None,
    }


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
        start: int = 0, snippet_chars: int = 500, space_key: str | None = None,
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
        if space_key is not None:
            clause += f' AND space = {json.dumps(self._space_key(space_key), ensure_ascii=False)}'
        raw, charset = self._get(
            f'{self.settings.base_url}/rest/api/search',
            params={'cql': f'type = page AND {clause}', 'limit': str(limit),
                    'start': str(start), 'excerpt': 'highlight',
                    'expand': 'content.history,content.version,content.space'},
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
                     **page_metadata(content),
                     'matches': matches[:snippet_chars],
                     'truncated': len(title) > 300 or len(matches) > snippet_chars}
            output['pages'].append(entry)
            # Reserve room for pagination metadata. Includes JSON escaping overhead.
            if len(output['pages']) == 1:
                while len(json.dumps(output)) > 15_500 and entry['matches']:
                    entry['matches'] = entry['matches'][:len(entry['matches']) // 2]
                    entry['truncated'] = True
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

    @staticmethod
    def _space_key(space_key: str) -> str:
        if (not isinstance(space_key, str) or not space_key.strip()
                or len(space_key) > 255 or space_key in ('.', '..')
                or any(ord(c) < 32 for c in space_key)):
            raise ConfluenceError('space_key must contain 1–255 non-control characters.')
        return space_key

    @staticmethod
    def _pagination(limit: int, start: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ConfluenceError('limit must be an integer between 1 and 20.')
        if type(start) is not int or not 0 <= start <= 1_000_000:
            raise ConfluenceError('start must be an integer between 0 and 1000000.')

    def _json(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        raw, charset = self._get(self.settings.base_url + path, params=params, accept='application/json')
        try:
            result = json.loads(raw.decode(charset))
        except (ValueError, UnicodeError, LookupError) as exc:
            raise ConfluenceError('Confluence REST API did not return valid JSON.') from exc
        if not isinstance(result, dict):
            raise ConfluenceError('Unexpected Confluence REST API response.')
        return result

    def _summary(self, content: dict[str, Any]) -> dict[str, Any]:
        page_id = str(content.get('id', ''))
        if not re.fullmatch(r'[1-9]\d{0,29}', page_id):
            raise ConfluenceError('Invalid page ID in Confluence response.')
        title = str(content.get('title') or '')
        fallback = f'{self.settings.base_url}/pages/viewpage.action?pageId={page_id}'
        webui = (content.get('_links') or {}).get('webui')
        url = urljoin(self.settings.base_url + '/', webui) if isinstance(webui, str) else fallback
        self.settings.validate_url(url)
        return {'page_id': page_id, 'title': title[:300],
                'url': url if len(json.dumps(url)) <= 4000 else fallback,
                **page_metadata(content), 'truncated': len(title) > 300}

    def _collection(self, data: dict[str, Any], key: str, limit: int, start: int,
                    transform: Any) -> dict[str, Any]:
        results = data.get('results')
        if not isinstance(results, list):
            raise ConfluenceError('Unexpected Confluence collection response.')
        output: dict[str, Any] = {key: [], 'has_more': False, 'next_start': None, 'truncated': False}
        consumed = 0
        for item in results[:limit]:
            if not isinstance(item, dict):
                raise ConfluenceError('Unexpected Confluence collection item.')
            entry = transform(item)
            if entry is not None:
                output[key].append(entry)
                if len(json.dumps(output)) > 15_500:
                    output[key].pop()
                    if consumed == 0:
                        raise ConfluenceError('Collection item exceeds response budget.')
                    output['truncated'] = True
                    break
            consumed += 1
        output['has_more'] = bool(consumed < len(results) or (data.get('_links') or {}).get('next')
                                  or len(results) >= limit)
        output['next_start'] = start + consumed if output['has_more'] else None
        output['truncated'] |= any(item.get('truncated', False) for item in output[key])
        return output

    def search_spaces(self, query: str = '', limit: int = 20, start: int = 0) -> dict[str, Any]:
        """Filter one bounded batch of spaces by name, key or ID; never scan automatically."""
        self._pagination(limit, start)
        if not isinstance(query, str) or len(query) > 500:
            raise ConfluenceError('query must contain at most 500 characters.')
        needle = query.strip().casefold()
        data = self._json('/rest/api/space', {'limit': str(limit), 'start': str(start)})

        def match(item: dict[str, Any]) -> dict[str, Any] | None:
            if not any(needle in str(item.get(field) or '').casefold() for field in ('name', 'key', 'id')):
                return None
            result = space_metadata(item)
            if result is None:
                raise ConfluenceError('Unexpected empty space.')
            return {**result, 'truncated': any(len(str(item.get(k) or '')) > n
                                              for k, n in (('id', 30), ('key', 255), ('name', 300)))}

        return self._collection(data, 'spaces', limit, start, match)

    def get_space_pages(self, space_key: str, limit: int = 10, start: int = 0) -> dict[str, Any]:
        self._pagination(limit, start)
        key = quote(self._space_key(space_key), safe='')
        data = self._json(f'/rest/api/space/{key}/content/page',
                          {'limit': str(limit), 'start': str(start), 'depth': 'all',
                           'expand': 'history,version,space'})
        return self._collection(data, 'pages', limit, start, self._summary)

    def get_page_children(self, page_id: str, limit: int = 10, start: int = 0) -> dict[str, Any]:
        self._pagination(limit, start)
        if not isinstance(page_id, str) or not re.fullmatch(r'[1-9]\d{0,29}', page_id):
            raise ConfluenceError('page_id must be a positive decimal integer.')
        data = self._json(f'/rest/api/content/{page_id}/child/page',
                          {'limit': str(limit), 'start': str(start), 'expand': 'history,version,space'})
        return self._collection(data, 'pages', limit, start, self._summary)

    def get_page_parent(self, page_id: str) -> dict[str, Any]:
        content = self._content(page_id, 'ancestors')
        ancestors = content.get('ancestors')
        if not isinstance(ancestors, list):
            raise ConfluenceError('Confluence did not return page ancestors.')
        if not ancestors:
            return {'parent': None}
        if not isinstance(ancestors[-1], dict):
            raise ConfluenceError('Unexpected page ancestor.')
        parent_id = str(ancestors[-1].get('id', ''))
        return {'parent': self._summary(self._content(parent_id, 'history,version,space'))}

    def _content(self, page_id: str | int, expand: str) -> dict[str, Any]:
        page_id = str(page_id)
        if not re.fullmatch(r'[1-9]\d*', page_id):
            raise ConfluenceError('page_id must be a positive decimal integer.')
        url = f'{self.settings.base_url}/rest/api/content/{page_id}'
        raw, charset = self._get(url, params={'expand': expand}, accept='application/json')
        try:
            import json
            result = json.loads(raw.decode(charset))
        except (ValueError, UnicodeError, LookupError) as exc:
            raise ConfluenceError('Confluence REST API did not return valid JSON; check the URL and authentication.') from exc
        if not isinstance(result, dict):
            raise ConfluenceError('Unexpected Confluence REST API response.')
        if result.get('type') not in (None, 'page'):
            raise ConfluenceError('The specified ID does not refer to a Confluence page.')
        return result

    def by_id(self, page_id: str | int) -> dict[str, Any]:
        result = self._content(page_id, 'body.view,body.storage,version,history,space')
        page_id = str(page_id)
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
            **page_metadata(result),
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
        metadata = self._content(actual_id, 'history,version,space') if actual_id else {}
        return {
            'page_id': actual_id,
            'title': title,
            'url': url,
            'source': 'web_html',
            'version': (metadata.get('version') or {}).get('number'),
            **page_metadata(metadata),
            'macros': [],
            **parse_content(str(content), url),
            'note': 'HTML and media references are returned, but JavaScript is not executed; interactive macros, diagrams and images may require a browser or separate authenticated retrieval.',
        }

