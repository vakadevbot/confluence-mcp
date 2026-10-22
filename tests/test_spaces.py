import json

import httpx
import pytest

from confluence_client import ConfluenceError, ConfluenceReader, Settings

BASE = 'https://wiki.example.org/confluence'
SPACE = {'id': 42, 'key': 'DEV', 'name': 'Development'}


def reader(handler):
    return ConfluenceReader(Settings(BASE, 'u', 'p', request_interval_ms=0), httpx.MockTransport(handler))


def page(number=1):
    return {'id': str(number), 'title': 'Page', 'space': SPACE,
            'body': {'view': {'value': '<p>Content</p>'}}}


@pytest.mark.parametrize('method,arg', [('by_id', '1'), ('by_url', BASE + '/pages/1'),
                                       ('search_pages', 'test')])
def test_space_returned_in_page_tools(method, arg):
    def handler(request):
        if method == 'search_pages':
            assert 'content.space' in request.url.params['expand']
            return httpx.Response(200, json={'results': [{'content': page()}]})
        assert 'space' in request.url.params['expand'].split(',')
        return httpx.Response(200, json=page())
    result = getattr(reader(handler), method)(arg)
    if method == 'search_pages':
        result = result['pages'][0]
    assert result['space'] == {'id': '42', 'key': 'DEV', 'name': 'Development'}


def test_scoped_search_escapes_key():
    key = 'DEV" OR type = attachment'
    def handler(request):
        assert request.url.params['cql'].endswith(' AND space = ' + json.dumps(key))
        return httpx.Response(200, json={'results': []})
    reader(handler).search_pages('hello', space_key=key)


@pytest.mark.parametrize('query', ['develop', 'dev', '42', ''])
def test_space_search(query):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path == '/confluence/rest/api/space'
        assert request.url.params['start'] == '10'
        return httpx.Response(200, json={'results': [SPACE]})
    result = reader(handler).search_spaces(query, start=10)
    assert result['spaces'] == [{'id': '42', 'key': 'DEV', 'name': 'Development', 'truncated': False}]
    assert not result['has_more']
    assert len(calls) == 1


def test_space_filter_advances_over_nonmatching_batch():
    result = reader(lambda _: httpx.Response(200, json={
        'results': [SPACE, SPACE], '_links': {'next': '/next'},
    })).search_spaces('missing', limit=2, start=4, max_batches=1)
    assert result['spaces'] == []
    assert result['next_start'] == 6
    assert result['has_more']
    assert result['scan_limit_reached']


@pytest.mark.parametrize('method,arg,path', [
    ('get_space_pages', 'DEV', '/confluence/rest/api/space/DEV/content/page'),
    ('get_page_children', '123', '/confluence/rest/api/content/123/child/page'),
])
def test_page_collections(method, arg, path):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path == path
        assert request.url.params['start'] == '3'
        assert request.url.params['limit'] == '2'
        assert request.url.params['expand'] == 'history,version,space'
        if method == 'get_space_pages':
            assert request.url.params['depth'] == 'all'
        return httpx.Response(200, json={'results': [page(1), page(2)], '_links': {'next': '/next'}})
    result = getattr(reader(handler), method)(arg, limit=2, start=3)
    assert result['next_start'] == 5
    assert result['pages'][0]['space']['id'] == '42'
    assert result['pages'][0]['url'] == BASE + '/pages/viewpage.action?pageId=1'
    assert 'body' not in result['pages'][0]
    assert len(calls) == 1


def test_immediate_parent_is_last_ancestor():
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path.endswith('/3'):
            assert request.url.params['expand'] == 'ancestors'
            return httpx.Response(200, json={'ancestors': [{'id': '1'}, {'id': '2'}]})
        assert request.url.path.endswith('/2')
        assert request.url.params['expand'] == 'history,version,space'
        return httpx.Response(200, json=page(2))
    result = reader(handler).get_page_parent('3')
    assert result['parent']['page_id'] == '2'
    assert result['parent']['space']['name'] == 'Development'
    assert len(calls) == 2


def test_root_parent():
    assert reader(lambda _: httpx.Response(200, json={'ancestors': []})).get_page_parent('1') == {'parent': None}


@pytest.mark.parametrize('method,arg', [('get_space_pages', 'DEV'), ('get_page_children', '1'),
                                       ('search_spaces', '')])
@pytest.mark.parametrize('params', [{'limit': 0}, {'limit': 21}, {'start': -1}, {'limit': True}])
def test_collection_validation_before_network(method, arg, params):
    def handler(request):
        pytest.fail('No network for invalid input')
    with pytest.raises(ConfluenceError):
        getattr(reader(handler), method)(arg, **params)


@pytest.mark.parametrize('method,arg', [('get_space_pages', ''), ('get_space_pages', '..'),
                                       ('get_page_children', '../2'), ('get_page_parent', 'abc')])
def test_invalid_identifiers(method, arg):
    with pytest.raises(ConfluenceError):
        getattr(reader(lambda _: pytest.fail('No network')), method)(arg)


@pytest.mark.parametrize('method,arg', [('get_space_pages', 'DEV'), ('get_page_children', '1'),
                                       ('search_spaces', '')])
def test_malformed_collections(method, arg):
    with pytest.raises(ConfluenceError):
        getattr(reader(lambda _: httpx.Response(200, json={'results': 'invalid'})), method)(arg)


def test_collection_budget_and_resume():
    pages = [{**page(n), 'title': 'я' * 300, 'space': {**SPACE, 'name': 'я' * 300}}
             for n in range(1, 21)]
    result = reader(lambda _: httpx.Response(200, json={'results': pages})).get_space_pages('DEV', limit=20)
    assert 0 < len(result['pages']) < 20
    assert result['next_start'] == len(result['pages'])
    assert result['truncated']
    assert len(json.dumps(result)) < 16000


def test_space_search_budget_counts_skipped_rows():
    spaces = [{'id': n, 'key': 'KEY', 'name': 'я' * 300} for n in range(1, 21)]
    spaces[0]['name'] = 'skip'
    result = reader(lambda _: httpx.Response(200, json={'results': spaces})).search_spaces('я', limit=20, start=10)
    assert result['next_start'] == 11 + len(result['spaces'])
    assert result['truncated']
    assert len(json.dumps(result)) < 16000


def test_large_search_hit_with_space_metadata_still_makes_progress():
    content = {**page(), 'title': 'я' * 300,
               'space': {'id': '42', 'key': 'я' * 255, 'name': 'я' * 300},
               '_links': {'webui': '/confluence/' + 'я' * 600},
               'history': {'createdBy': {'displayName': 'я' * 128, 'accountId': 'я' * 64}},
               'version': {'by': {'displayName': 'я' * 128, 'accountId': 'я' * 64}}}
    result = reader(lambda _: httpx.Response(200, json={
        'results': [{'content': content, 'excerpt': 'я' * 1000}]})).search_pages('я', snippet_chars=1000)
    assert len(result['pages']) == 1
    assert result['truncated']
    assert len(json.dumps(result)) < 16000
