import httpx
import pytest

from confluence_client import ConfluenceError, ConfluenceReader, Settings, page_id_from_url, parse_content


BASE = 'https://wiki.example.org/confluence'
SETTINGS = Settings(BASE, 'ivan', 'password')


def reader(handler, settings=SETTINGS):
    return ConfluenceReader(settings, transport=httpx.MockTransport(handler))


def test_page_by_id_renders_html_and_macros():
    def handler(request):
        assert request.method == 'GET'
        assert request.url.path == '/confluence/rest/api/content/123'
        assert request.url.params['expand'] == 'body.view,body.storage,version'
        assert request.headers['authorization'].startswith('Basic ')
        return httpx.Response(200, json={
            'id': '123', 'type': 'page', 'title': 'Example',
            '_links': {'webui': '/confluence/pages/viewpage.action?pageId=123'},
            'version': {'number': 8},
            'body': {
                'view': {'value': '<h2>Hello</h2><p>World</p><img src="/confluence/download/a.png" alt="diagram">'
                                  '<svg><circle r="1"/></svg><script>secret_code()</script>'},
                'storage': {'value': '<ac:structured-macro ac:name="plantuml" ac:macro-id="abc">'
                                     '</ac:structured-macro>'},
            },
        })

    result = reader(handler).by_id('123')
    assert result['page_id'] == '123'
    assert result['title'] == 'Example'
    assert result['version'] == 8
    assert result['source'] == 'rest_body_view'
    assert result['images'][0]['url'] == 'https://wiki.example.org/confluence/download/a.png'
    assert result['svg_count'] == 1
    assert result['script_count'] == 1
    assert 'secret_code' not in result['text']
    assert 'secret_code' in result['html']
    assert result['macros'][0]['name'] == 'plantuml'


@pytest.mark.parametrize('url,page_id', [
    (BASE + '/pages/viewpage.action?pageId=123', '123'),
    (BASE + '/spaces/DEV/pages/456/Title', '456'),
    (BASE + '/rest/api/content/456', '456'),
    (BASE + '/display/DEV/Page+Title', None),
])
def test_extract_page_id(url, page_id):
    assert page_id_from_url(url) == page_id


def test_slug_url_uses_html_fallback():
    def handler(request):
        assert request.url.path == '/confluence/display/DEV/Test'
        return httpx.Response(
            200, text='<html><head><meta name="ajs-page-id" content="456"></head>'
                      '<body><h1>Page</h1><div id="main-content"><div class="wiki-content">'
                      '<p>Visible content</p><iframe src="/confluence/embed"></iframe>'
                      '</div></div></body></html>',
        )
    result = reader(handler).by_url(BASE + '/display/DEV/Test')
    assert result['source'] == 'web_html'
    assert result['page_id'] == '456'
    assert result['text'].strip() == 'Visible content'
    assert result['embedded_media'][0]['url'] == 'https://wiki.example.org/confluence/embed'


def test_outside_urls_refused_before_network_access():
    def handler(request):
        pytest.fail('Network should not be touched')
    for url in ['https://evil.example.org/confluence/pages/viewpage.action?pageId=1',
                'https://wiki.example.org/other/pages/viewpage.action?pageId=1',
                'http://wiki.example.org/confluence/pages/viewpage.action?pageId=1']:
        with pytest.raises(ConfluenceError):
            reader(handler).by_url(url)


def test_cross_origin_redirect_refused():
    def handler(request):
        return httpx.Response(302, headers={'Location': 'https://evil.example.org/steal'})
    with pytest.raises(ConfluenceError, match='origin'):
        reader(handler).by_url(BASE + '/display/DEV/Page')


def test_authentication_error_is_clear():
    def handler(request):
        return httpx.Response(401)
    with pytest.raises(ConfluenceError, match='401'):
        reader(handler).by_id(33)


def test_too_large_page_refused():
    def handler(request):
        return httpx.Response(200, text='a' * 100)
    with pytest.raises(ConfluenceError, match='MAX_PAGE_BYTES'):
        reader(handler, Settings(BASE, 'ivan', 'pass', max_page_bytes=50)).by_url(BASE + '/display/DEV/Big')


def test_html_text_removes_script_and_preserves_html():
    source = '<div><p>Text</p><script>alert(1)</script><img src="x.png" alt="D"></div>'
    parsed = parse_content(source, BASE + '/display/X/Y')
    assert 'alert(1)' in parsed['html']
    assert 'alert(1)' not in parsed['text']
    assert parsed['images'][0]['url'] == BASE + '/display/X/x.png'
