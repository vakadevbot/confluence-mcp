import json

import httpx
import pytest

from confluence_client import ConfluenceReader, Settings, page_metadata


@pytest.mark.parametrize('id_field', ['accountId', 'userKey', 'username'])
@pytest.mark.parametrize('method', ['by_id', 'by_url', 'search_pages'])
def test_metadata_for_cloud_and_server(id_field, method):
    content = {
        'id': '123', 'type': 'page', 'title': 'Example',
        'history': {'createdBy': {'displayName': 'Creator', id_field: 'creator-id'},
                    'createdDate': '2025-01-02T03:04:05.000+0300'},
        'version': {'number': 4, 'by': {'displayName': 'Editor', id_field: 'editor-id'},
                    'when': '2026-10-21T12:00:00.000+0300'},
        'body': {'view': {'value': '<p>Example</p>'}},
    }
    calls = []
    def handler(request):
        calls.append(request)
        if method == 'search_pages':
            return httpx.Response(200, json={'results': [{'content': content, 'excerpt': 'Example'}]})
        return httpx.Response(200, json=content)
    reader = ConfluenceReader(Settings('https://wiki.example.org', 'u', 'p', request_interval_ms=0),
                              httpx.MockTransport(handler))
    argument = {'by_id': '123', 'by_url': 'https://wiki.example.org/pages/123',
                'search_pages': 'Example'}[method]
    result = getattr(reader, method)(argument)
    if method == 'search_pages':
        result = result['pages'][0]
    assert result['created_by'] == {'display_name': 'Creator', 'id': 'creator-id'}
    assert result['last_modified_by'] == {'display_name': 'Editor', 'id': 'editor-id'}
    assert result['created_at'] == content['history']['createdDate']
    assert result['last_modified_at'] == content['version']['when']
    assert len(calls) == 1


def test_missing_metadata_is_null():
    assert page_metadata({}) == dict(space=None, created_by=None, created_at=None,
                                     last_modified_by=None, last_modified_at=None)
    assert page_metadata({'history': {'createdBy': {}}})['created_by'] is None


def test_search_metadata_stays_within_budget():
    content = {'id': '1', 'title': 'я' * 300,
               'history': {'createdBy': {'displayName': 'я' * 10000, 'accountId': 'я' * 10000}},
               'version': {'by': {'displayName': 'я' * 10000, 'accountId': 'я' * 10000}}}
    payload = {'results': [{'content': content, 'excerpt': 'я' * 1000}] * 20}
    reader = ConfluenceReader(Settings('https://wiki.example.org', 'u', 'p', request_interval_ms=0),
                              httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    result = reader.search_pages('я', limit=20, snippet_chars=1000)
    assert 0 < len(result['pages']) < 20
    assert len(json.dumps(result)) < 16000
    assert result['next_start'] == len(result['pages'])


def test_html_without_page_id_returns_null_metadata():
    reader = ConfluenceReader(Settings('https://wiki.example.org', 'u', 'p', request_interval_ms=0),
        httpx.MockTransport(lambda _: httpx.Response(200, text='<article>Example</article>')))
    result = reader.by_url('https://wiki.example.org/display/DEV/Example')
    assert result['created_by'] is None
    assert result['last_modified_at'] is None
