import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import httpx
import pytest

import confluence_client as module
from confluence_client import ConfluenceError, ConfluenceReader, Settings

BASE = 'https://wiki.example.org/confluence'
SETTINGS = Settings(BASE, 'user', 'pass', request_interval_ms=0)


def page(number=1, **kwargs):
    return {'content': {'id': str(number), 'title': 'Deployment',
                        '_links': {'webui': f'/confluence/pages/{number}/Deployment'}},
            'excerpt': '<p>Use @@@hl@@@deployment@@@endhl@@@ safely</p>', **kwargs}


def reader(payload):
    return ConfluenceReader(SETTINGS, httpx.MockTransport(
        lambda request: httpx.Response(200, json=payload)))


@pytest.mark.parametrize('search_in,field', [('title', 'title'), ('content', 'text'), ('all', 'title')])
def test_search_request_and_excerpts(search_in, field):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == '/confluence/rest/api/search'
        assert request.url.params['cql'] == (
            'type = page AND (title ~ "\\"deployment\\"" OR text ~ "\\"deployment\\"")'
            if search_in == 'all' else f'type = page AND {field} ~ "\\"deployment\\""')
        assert dict(request.url.params)['excerpt'] == 'highlight'
        assert request.url.params['limit'] == '5'
        assert request.url.params['start'] == '10'
        assert 'expand' not in request.url.params
        return httpx.Response(200, json={'results': [page()], '_links': {'next': '/next'}})

    result = ConfluenceReader(SETTINGS, httpx.MockTransport(handler)).search_pages(
        'deployment', search_in=search_in, start=10)
    assert len(requests) == 1
    assert result['pages'][0] == {
        'page_id': '1', 'title': 'Deployment', 'url': BASE + '/pages/1/Deployment',
        'matches': 'Use deployment safely', 'truncated': False,
    }
    assert result['next_start'] == 11
    assert result['has_more'] is True


def test_literal_cql_escaping():
    def handler(request):
        cql = request.url.params['cql']
        assert cql.startswith('type = page AND title ~ "\\"')
        assert '\\\\\\"' in cql  # User quote remains escaped inside both language layers.
        assert '\\\\*' in cql  # No wildcard expansion.
        return httpx.Response(200, json={'results': []})
    ConfluenceReader(SETTINGS, httpx.MockTransport(handler)).search_pages(
        '" OR type = attachment OR title ~ "*', search_in='title')


def test_budget_unicode_and_explicit_pagination():
    payload = {'results': [page(n, excerpt='я' * 5000) for n in range(1, 21)]}
    result = reader(payload).search_pages('я', limit=20, start=7, snippet_chars=1000)
    assert len(json.dumps(result)) <= 16000
    assert 0 < len(result['pages']) < 20
    assert result['next_start'] == 7 + len(result['pages'])
    assert result['has_more'] and result['truncated']
    assert all(len(p['matches']) == 1000 for p in result['pages'])


def test_empty_and_missing_excerpt():
    assert reader({'results': []}).search_pages('hello') == {
        'pages': [], 'has_more': False, 'next_start': None, 'truncated': False}
    result = reader({'results': [page(excerpt='')]}).search_pages('hello')
    assert result['pages'][0]['matches'] == ''
    assert result['next_start'] is None


@pytest.mark.parametrize('excerpt', [
    'test-int-api.example.org',
    'test<span>-int-</span>api.example.org',
    '<a href="https://test-int-api.example.org">test-<em>int</em>-api.example.org</a>',
    'test&#45;int&#x2d;api.example.org',
    '@@@hl@@@test@@@endhl@@@-@@@hl@@@int@@@endhl@@@-api.example.org',
    '<span>@@@hl@@@test@@@endhl@@@</span>-<b>int</b>-api.example.org',
])
def test_search_preserves_hyphenated_hostname(excerpt):
    result = reader({'results': [page(excerpt=excerpt)]}).search_pages('test-int-api')
    assert result['pages'][0]['matches'] == 'test-int-api.example.org'


def test_excerpt_preserves_block_boundaries_and_literal_punctuation():
    excerpt = '<p>test-<b>int</b>-api.example.org</p><p>--flag a_b /v1/test-api</p>'
    result = reader({'results': [page(excerpt=excerpt)]}).search_pages('test')
    assert result['pages'][0]['matches'] == 'test-int-api.example.org --flag a_b /v1/test-api'


def test_search_does_not_invent_missing_hyphens():
    result = reader({'results': [page(excerpt='testintapi')]}).search_pages('test-int-api')
    assert result['pages'][0]['matches'] == 'testintapi'


@pytest.mark.parametrize('kwargs', [
    {'query': ''}, {'query': ' '}, {'query': 'x' * 501}, {'search_in': 'cql'},
    {'limit': 21}, {'limit': 0}, {'limit': True}, {'start': -1},
    {'snippet_chars': 1001}, {'snippet_chars': 49},
])
def test_invalid_search_never_calls_network(kwargs):
    def handler(request):
        pytest.fail('Invalid input must not reach network')
    with pytest.raises(ConfluenceError):
        ConfluenceReader(SETTINGS, httpx.MockTransport(handler)).search_pages(
            **({'query': 'hello'} | kwargs))


@pytest.mark.parametrize('payload', [{}, {'results': 'bad'}, {'results': [None]},
                                      {'results': [{'content': {'id': '../bad'}}]}])
def test_invalid_search_response(payload):
    with pytest.raises(ConfluenceError):
        reader(payload).search_pages('hello')


def test_search_reuses_http_guards():
    for response, message in [(httpx.Response(401), '401'),
                              (httpx.Response(200, text='<html>login</html>'), 'JSON')]:
        with pytest.raises(ConfluenceError, match=message):
            ConfluenceReader(SETTINGS, httpx.MockTransport(lambda _: response)).search_pages('hi')
    with pytest.raises(ConfluenceError, match='MAX_PAGE_BYTES'):
        ConfluenceReader(replace(SETTINGS, max_page_bytes=10), httpx.MockTransport(
            lambda _: httpx.Response(200, text='x' * 100))).search_pages('hi')


def test_long_url_and_script_excerpt_are_safe():
    item = page(excerpt='<script>secret</script><b>hello</b>')
    item['content']['_links']['webui'] = '/confluence/' + 'я' * 2000
    result = reader({'results': [item]}).search_pages('hello')
    assert result['pages'][0]['url'] == BASE + '/pages/viewpage.action?pageId=1'
    assert result['pages'][0]['matches'] == 'hello'


def test_env_interval(monkeypatch):
    monkeypatch.setenv('CONFLUENCE_BASE_URL', BASE)
    monkeypatch.setenv('CONFLUENCE_USERNAME', 'user')
    monkeypatch.setenv('CONFLUENCE_PASSWORD', 'pass')
    monkeypatch.delenv('CONFLUENCE_REQUEST_INTERVAL_MS', raising=False)
    assert Settings.from_env().request_interval_ms == 1000
    monkeypatch.setenv('CONFLUENCE_REQUEST_INTERVAL_MS', '250')
    assert Settings.from_env().request_interval_ms == 250
    for bad in ('-1', '1.5', 'nan', 'abc'):
        monkeypatch.setenv('CONFLUENCE_REQUEST_INTERVAL_MS', bad)
        with pytest.raises(ConfluenceError):
            Settings.from_env()


def test_rate_limit_shared_across_tools_readers_and_redirects(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(module, '_last_request_at', None)
    starts = []

    def handler(request):
        starts.append(clock[0])
        if request.url.path.endswith('/redirect'):
            return httpx.Response(302, headers={'Location': BASE + '/rest/api/search'})
        return httpx.Response(200, json={'results': []})

    settings = replace(SETTINGS, request_interval_ms=1000)
    first = ConfluenceReader(settings, httpx.MockTransport(handler))
    second = ConfluenceReader(settings, httpx.MockTransport(handler))
    first._get(BASE + '/redirect')
    second.search_pages('hello')
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: ConfluenceReader(settings, httpx.MockTransport(handler)).search_pages('hi'), range(3)))
    assert starts == [0, 1, 2, 3, 4, 5]


def test_zero_interval_does_not_sleep(monkeypatch):
    monkeypatch.setattr(module, '_last_request_at', None)
    monkeypatch.setattr(module.time, 'sleep', lambda _: pytest.fail('Unexpected delay'))
    reader({'results': []}).search_pages('one')
    reader({'results': []}).search_pages('two')
