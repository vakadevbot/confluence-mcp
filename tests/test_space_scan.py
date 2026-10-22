import json

import httpx
import pytest

from confluence_client import ConfluenceError
from test_spaces import reader


def catalog(count, matches):
    return [{'id': i + 1, 'key': f'S{i + 1}', 'name': matches.get(i, f'Space {i}')}
            for i in range(count)]


def api(spaces, calls, cap=100):
    def handler(request):
        start = int(request.url.params['start'])
        limit = int(request.url.params['limit'])
        calls.append((start, limit))
        batch = spaces[start:start + min(limit, cap)]
        end = start + len(batch)
        links = {'next': f'/rest/api/space?start={end}'} if end < len(spaces) else {}
        return httpx.Response(200, json={'results': batch, 'start': start, '_links': links})
    return handler


def test_finds_russian_name_beyond_first_batch_of_145():
    calls = []
    spaces = catalog(145, {140: 'Технологическая\u00a0платформа'})
    spaces[140]['key'] = 'SA17'
    result = reader(api(spaces, calls)).search_spaces('  технологическая   ПЛАТФОРМА ')
    assert [s['key'] for s in result['spaces']] == ['SA17']
    assert calls == [(0, 100), (100, 100)]
    assert result['scanned'] == 145
    assert not result['has_more']
    assert not result['scan_limit_reached']


def test_no_matches_means_all_batches_checked():
    calls = []
    result = reader(api(catalog(145, {}), calls)).search_spaces('missing')
    assert result['spaces'] == []
    assert result['scanned'] == 145
    assert not result['has_more']
    assert result['next_start'] is None
    assert len(calls) == 2


def test_match_limit_counts_matches_not_scanned_spaces_and_resumes():
    calls = []
    client = reader(api(catalog(145, {22: 'Target A', 110: 'Target B', 139: 'Target C'}), calls))
    first = client.search_spaces('target', limit=2)
    assert [s['id'] for s in first['spaces']] == ['23', '111']
    assert first['scanned'] == 111
    assert first['next_start'] == 111
    second = client.search_spaces('target', limit=2, start=first['next_start'])
    assert [s['id'] for s in second['spaces']] == ['140']
    assert not second['has_more']


def test_scan_budget_marks_incomplete_and_can_resume():
    calls = []
    client = reader(api(catalog(245, {220: 'Target'}), calls))
    first = client.search_spaces('target', max_batches=2)
    assert first['spaces'] == []
    assert first['has_more'] and first['scan_limit_reached']
    assert first['next_start'] == 200
    assert first['scanned'] == 200
    second = client.search_spaces('target', start=200)
    assert second['spaces'][0]['id'] == '221'
    assert not second['has_more']


def test_server_caps_batches_but_next_link_continues_search():
    calls = []
    result = reader(api(catalog(145, {140: 'Target'}), calls, cap=25)).search_spaces('target')
    assert result['spaces'][0]['id'] == '141'
    assert [start for start, _ in calls] == [0, 25, 50, 75, 100, 125]


def test_empty_query_is_still_one_listing_batch():
    calls = []
    result = reader(api(catalog(145, {}), calls)).search_spaces('', limit=5)
    assert calls == [(0, 5)]
    assert len(result['spaces']) == 5
    assert result['next_start'] == 5
    assert not result['scan_limit_reached']


def test_response_budget_resumes_at_unreturned_match():
    calls = []
    spaces = catalog(50, {i: 'я' * 300 for i in range(5, 50)})
    client = reader(api(spaces, calls))
    result = client.search_spaces('я', limit=20)
    assert result['truncated']
    assert len(json.dumps(result)) < 16000
    assert result['next_start'] == 5 + len(result['spaces'])
    next_result = client.search_spaces('я', start=result['next_start'])
    assert int(next_result['spaces'][0]['id']) == int(result['spaces'][-1]['id']) + 1


@pytest.mark.parametrize('max_batches', [0, 21, True, 1.5])
def test_invalid_scan_budget(max_batches):
    with pytest.raises(ConfluenceError):
        reader(lambda _: pytest.fail('No network')).search_spaces('test', max_batches=max_batches)


@pytest.mark.parametrize('payload', [
    {'results': [], '_links': {'next': '?start=0'}},
    {'results': [{'id': 1}], '_links': {'next': '?start=0'}},
    {'results': [{'id': 1}], '_links': {'next': '?start=bad'}},
    {'results': [{'id': 1}], 'start': 99},
])
def test_broken_pagination_does_not_loop_or_claim_no_matches(payload):
    with pytest.raises(ConfluenceError):
        reader(lambda _: httpx.Response(200, json=payload)).search_spaces('missing')
