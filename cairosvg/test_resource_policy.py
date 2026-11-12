"""
Tests for the ResourcePolicy API used when converting untrusted SVG.

"""

import base64
import io
import os
from types import SimpleNamespace

import pytest
from defusedxml.common import EntitiesForbidden
from PIL import Image

from . import (
    ResourceAccessError, ResourcePolicy, surface, svg2eps, svg2pdf, svg2png,
    svg2ps, svg2svg)

PNG_MAGIC = b'\211PNG\r\n\032\n'

RECT_SVG = b'''<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <rect width="10" height="10" fill="red"/>
</svg>'''

MAIN_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">
  <defs>
    <linearGradient id="grad">
      <stop offset="0" stop-color="red"/>
      <stop offset="1" stop-color="blue"/>
    </linearGradient>
    <rect id="local" width="10" height="10" fill="url(#grad)"/>
  </defs>
  <style>@import "sub/style.css";</style>
  <use href="#local" x="0" y="0"/>
  <use href="sub/inner.svg#shape" x="15" y="0"/>
  <image href="sub/pic.png" x="30" y="0" width="10" height="10"/>
  <image href="{data_url}" x="45" y="0" width="10" height="10"/>
  <rect class="ext" x="60" y="0" width="10" height="10"/>
  <rect class="ext2" x="75" y="0" width="10" height="10"/>
</svg>'''

INNER_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <rect id="shape" width="10" height="10" fill="blue"/>
</svg>'''

NESTED_HTTP_IMAGE_SVG = '''<svg xmlns="http://www.w3.org/2000/svg"
     width="10" height="10">
  <image href="http://example.invalid/x.png" width="10" height="10"/>
</svg>'''

NESTED_HTTP_USE_SVG = '''<svg xmlns="http://www.w3.org/2000/svg"
     width="10" height="10">
  <g id="shape"><use href="http://example.invalid/x.svg#foo"/></g>
</svg>'''

NESTED_HTTP_CSS_SVG = '''<svg xmlns="http://www.w3.org/2000/svg"
     width="10" height="10">
  <style>@import "http://example.invalid/x.css";</style>
  <rect width="10" height="10" fill="red"/>
</svg>'''

NESTED_ESCAPE_SVG = '''<svg xmlns="http://www.w3.org/2000/svg"
     width="10" height="10">
  <image href="../outside/secret.png" width="10" height="10"/>
</svg>'''

XXE_SVG = b'''<?xml version="1.0"?>
<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
  <text x="1" y="1">&xxe;</text>
</svg>'''


def png_bytes(rgba=(255, 0, 0, 255)):
    """Return the bytes of a small valid PNG image."""
    file_obj = io.BytesIO()
    Image.new('RGBA', (4, 4), rgba).save(file_obj, 'PNG')
    return file_obj.getvalue()


def image_svg(href):
    """Return an SVG document embedding one image."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        f'<image href="{href}" x="0" y="0" width="10" height="10"/>'
        f'</svg>').encode()


@pytest.fixture
def no_network(monkeypatch):
    """Make any real network attempt fail loudly."""
    def boom(*args, **kwargs):
        raise AssertionError('a real network request was attempted')
    monkeypatch.setattr('cairosvg.url.urlopen', boom)
    import urllib.request
    monkeypatch.setattr(urllib.request, 'urlopen', boom)


class SpyFetcher:
    """A url_fetcher that records calls and serves nothing by itself."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply

    def __call__(self, url, resource_type):
        self.calls.append((url, resource_type))
        if self.reply is not None:
            return self.reply
        raise AssertionError(f'fetcher should not have been called: {url}')


@pytest.fixture
def assets(tmp_path):
    """Build a root directory with safe resources and an outside directory."""
    root = tmp_path / 'root'
    sub = root / 'sub'
    sub.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()

    red_png = png_bytes()
    (sub / 'pic.png').write_bytes(red_png)
    (outside / 'secret.png').write_bytes(png_bytes((1, 2, 3, 255)))
    (sub / 'inner.svg').write_text(INNER_SVG)
    (sub / 'style.css').write_text(
        '@import "more.css";\n.ext { fill: green; }\n')
    (sub / 'more.css').write_text('.ext2 { fill: #0000ff; }\n')
    data_url = 'data:image/png;base64,' + base64.b64encode(red_png).decode()
    (root / 'main.svg').write_text(MAIN_SVG.format(data_url=data_url))
    (root / 'nested_http_image.svg').write_text(NESTED_HTTP_IMAGE_SVG)
    (root / 'nested_http_use.svg').write_text(NESTED_HTTP_USE_SVG)
    (root / 'nested_http_css.svg').write_text(NESTED_HTTP_CSS_SVG)
    (root / 'nested_escape.svg').write_text(NESTED_ESCAPE_SVG)
    os.symlink(outside / 'secret.png', root / 'link.png')

    return SimpleNamespace(
        tmp_path=tmp_path, root=root, sub=sub, outside=outside,
        main=root / 'main.svg', red_png=red_png, data_url=data_url,
        outside_png=outside / 'secret.png')


def test_policy_allows_safe_nested_resources(assets, no_network):
    """Local in-root SVG, PNG, CSS, data URL and #fragments all work."""
    policy = ResourcePolicy(root=assets.root)
    output = assets.tmp_path / 'output.png'
    result = svg2png(
        url=str(assets.main), write_to=str(output), resource_policy=policy)
    assert result is None  # written to file
    content = output.read_bytes()
    assert content.startswith(PNG_MAGIC)

    # The PNG really contains the nested resources.
    rendered = Image.open(io.BytesIO(content)).convert('RGBA')
    assert rendered.size == (120, 40)

    def pixel(x, y):
        return rendered.getpixel((x, y))

    # Blue rectangle from the nested SVG loaded through <use>.
    assert pixel(20, 5)[2] > 200 and pixel(20, 5)[0] < 60
    # Red PNG loaded through <image>.
    assert pixel(35, 5)[0] > 200 and pixel(35, 5)[1] < 60
    # Red PNG loaded from a data: URL.
    assert pixel(47, 2)[0] > 200 and pixel(47, 2)[1] < 60
    # Green from the CSS file imported by the main document.
    r, g, b, _ = pixel(65, 5)
    assert 100 < g < 160 and r < 40 and b < 40
    # Blue from the CSS file imported by the first CSS file (base URL kept).
    assert pixel(80, 5)[2] > 200 and pixel(80, 5)[0] < 60


@pytest.mark.parametrize('convert, magic', [
    (svg2png, PNG_MAGIC),
    (svg2pdf, b'%PDF'),
    (svg2ps, b'%!'),
    (svg2eps, b'%!'),
    (svg2svg, b'<?xml'),
])
def test_all_formats_accept_resource_policy(tmp_path, convert, magic):
    """Every svg2* entry point takes the resource_policy parameter."""
    policy = ResourcePolicy(root=tmp_path)
    assert convert(
        bytestring=RECT_SVG, resource_policy=policy).startswith(magic)


@pytest.mark.parametrize('url', [
    'http://example.invalid/main.svg',
    'https://example.invalid/main.svg',
    'ftp://example.invalid/main.svg',
])
def test_network_root_document_rejected(tmp_path, no_network, url):
    """The root document URL itself cannot use the network."""
    spy = SpyFetcher()
    policy = ResourcePolicy(root=tmp_path)
    with pytest.raises(ResourceAccessError):
        surface.PNGSurface.convert(
            url=url, resource_policy=policy, url_fetcher=spy)
    # The policy rejects before the fetcher (or the network) is touched.
    assert spy.calls == []


def test_network_image_rejected(tmp_path, no_network):
    """An external HTTP image is rejected by policy, not by network failure."""
    spy = SpyFetcher()
    svg = image_svg('https://example.invalid/x.png')
    with pytest.raises(ResourceAccessError) as exc_info:
        surface.PNGSurface.convert(
            bytestring=svg, resource_policy=ResourcePolicy(root=tmp_path),
            url_fetcher=spy)
    assert 'protocol' in exc_info.value.reason
    assert spy.calls == []


def test_root_document_outside_root_rejected(assets):
    """The root document itself cannot be a file outside of the root."""
    doc = assets.outside / 'doc.svg'
    doc.write_bytes(RECT_SVG)
    with pytest.raises(ResourceAccessError) as exc_info:
        svg2png(
            url=str(doc),
            resource_policy=ResourcePolicy(root=assets.root))
    assert 'outside of allowed root' in exc_info.value.reason


def test_image_outside_root_rejected(assets):
    """A file outside of the root directory cannot be loaded."""
    svg = image_svg(str(assets.outside_png))
    with pytest.raises(ResourceAccessError) as exc_info:
        svg2png(
            bytestring=svg,
            resource_policy=ResourcePolicy(root=assets.root))
    assert 'outside of allowed root' in exc_info.value.reason
    assert str(assets.outside_png) in exc_info.value.url


def test_relative_traversal_outside_root_rejected(assets):
    """.. segments cannot escape the root directory."""
    doc = assets.sub / 'doc.svg'
    doc.write_text(image_svg('../../outside/secret.png').decode())
    with pytest.raises(ResourceAccessError):
        svg2png(url=str(doc), resource_policy=ResourcePolicy(root=assets.root))


def test_encoded_traversal_outside_root_rejected(assets):
    """Percent-encoded .. segments cannot escape the root directory."""
    doc = assets.sub / 'doc.svg'
    doc.write_text(
        image_svg('..%2F..%2Foutside%2Fsecret.png').decode())
    with pytest.raises(ResourceAccessError):
        svg2png(url=str(doc), resource_policy=ResourcePolicy(root=assets.root))


def test_symlink_outside_root_rejected(assets):
    """A symlink inside the root pointing outside of it is rejected."""
    doc = assets.root / 'doc.svg'
    doc.write_text(image_svg('link.png').decode())
    with pytest.raises(ResourceAccessError) as exc_info:
        svg2png(url=str(doc), resource_policy=ResourcePolicy(root=assets.root))
    assert 'outside of allowed root' in exc_info.value.reason


def test_non_local_file_host_rejected(tmp_path):
    """file:// URLs for non-local hosts are rejected."""
    svg = image_svg('file://example.invalid/etc/passwd')
    with pytest.raises(ResourceAccessError) as exc_info:
        svg2png(
            bytestring=svg,
            resource_policy=ResourcePolicy(root=tmp_path))
    assert 'not local' in exc_info.value.reason


def test_data_url_controlled_by_allow_data(tmp_path):
    """data: resources depend on allow_data, decoded size counts."""
    svg = image_svg(
        'data:image/png;base64,' +
        base64.b64encode(png_bytes()).decode())
    with pytest.raises(ResourceAccessError) as exc_info:
        svg2png(
            bytestring=svg,
            resource_policy=ResourcePolicy(root=tmp_path, allow_data=False))
    assert 'data:' in exc_info.value.reason
    assert svg2png(
        bytestring=svg,
        resource_policy=ResourcePolicy(root=tmp_path)).startswith(PNG_MAGIC)


def test_max_resource_bytes(assets):
    """Resources larger than max_resource_bytes are rejected."""
    big = assets.root / 'big.png'
    big.write_bytes(png_bytes() * 10)
    svg = image_svg('big.png')
    (assets.root / 'doc.svg').write_bytes(svg)
    policy = ResourcePolicy(root=assets.root, max_resource_bytes=64)
    with pytest.raises(ResourceAccessError) as exc_info:
        svg2png(url=str(assets.root / 'doc.svg'), resource_policy=policy)
    assert 'larger than 64 bytes' in exc_info.value.reason
    # A small enough file is fine.
    policy = ResourcePolicy(
        root=assets.root, max_resource_bytes=len(png_bytes()) * 10)
    assert svg2png(
        url=str(assets.root / 'doc.svg'),
        resource_policy=policy).startswith(PNG_MAGIC)


def test_max_resource_bytes_applies_to_decoded_data_url(tmp_path):
    """The size limit applies to the decoded data: payload."""
    payload = png_bytes()
    svg = image_svg(
        'data:image/png;base64,' + base64.b64encode(payload).decode())
    policy = ResourcePolicy(root=tmp_path, max_resource_bytes=len(payload) - 1)
    with pytest.raises(ResourceAccessError):
        svg2png(bytestring=svg, resource_policy=policy)


@pytest.mark.parametrize('value', [0, -1, '1024', 1.5, True])
def test_invalid_max_resource_bytes(tmp_path, value):
    """max_resource_bytes must be a positive integer."""
    with pytest.raises(ValueError):
        ResourcePolicy(root=tmp_path, max_resource_bytes=value)


def test_custom_fetcher_cannot_bypass_boundary(assets):
    """A custom fetcher is only called after the policy allows the URL."""
    spy = SpyFetcher(reply=assets.red_png)
    svg = image_svg(str(assets.outside_png))
    with pytest.raises(ResourceAccessError):
        surface.PNGSurface.convert(
            bytestring=svg, url_fetcher=spy,
            resource_policy=ResourcePolicy(root=assets.root))
    assert spy.calls == []


def test_custom_fetcher_result_size_checked(assets):
    """Bytes returned by a custom fetcher count towards the size limit."""
    spy = SpyFetcher(reply=b'x' * 1000)
    in_root = assets.root / 'pic.png'
    svg = image_svg(str(in_root))
    policy = ResourcePolicy(root=assets.root, max_resource_bytes=100)
    with pytest.raises(ResourceAccessError) as exc_info:
        surface.PNGSurface.convert(
            bytestring=svg, url_fetcher=spy, resource_policy=policy)
    assert 'larger than 100 bytes' in exc_info.value.reason
    # The URL was allowed, so the fetcher was called before the size check.
    assert len(spy.calls) == 1


def test_nested_svg_uses_same_policy(assets, no_network):
    """A nested SVG document cannot fetch HTTP resources."""
    svg = image_svg('nested_http_image.svg')
    (assets.root / 'doc.svg').write_bytes(svg)
    with pytest.raises(ResourceAccessError):
        svg2png(
            url=str(assets.root / 'doc.svg'),
            resource_policy=ResourcePolicy(root=assets.root))


def test_nested_use_uses_same_policy(assets, no_network):
    """A <use> reference inside a nested document cannot fetch HTTP."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<use href="nested_http_use.svg#shape"/></svg>')
    (assets.root / 'doc.svg').write_text(svg)
    with pytest.raises(ResourceAccessError):
        svg2png(
            url=str(assets.root / 'doc.svg'),
            resource_policy=ResourcePolicy(root=assets.root))


def test_nested_css_uses_same_policy(assets, no_network):
    """CSS inside a nested SVG cannot @import from the network."""
    svg = image_svg('nested_http_css.svg')
    (assets.root / 'doc.svg').write_bytes(svg)
    with pytest.raises(ResourceAccessError):
        svg2png(
            url=str(assets.root / 'doc.svg'),
            resource_policy=ResourcePolicy(root=assets.root))


def test_nested_file_escape_uses_same_policy(assets):
    """A nested SVG cannot read files outside of the root either."""
    svg = image_svg('nested_escape.svg')
    (assets.root / 'doc.svg').write_bytes(svg)
    with pytest.raises(ResourceAccessError):
        svg2png(
            url=str(assets.root / 'doc.svg'),
            resource_policy=ResourcePolicy(root=assets.root))


@pytest.mark.parametrize('rule', [
    '@import "http://example.invalid/x.css";',
    '@import url(http://example.invalid/x.css);',
])
def test_css_import_network_rejected(tmp_path, no_network, rule):
    """CSS @import cannot load stylesheets from the network."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        f'<style>{rule}</style>'
        '<rect width="10" height="10" fill="red"/></svg>').encode()
    with pytest.raises(ResourceAccessError):
        svg2png(
            bytestring=svg, resource_policy=ResourcePolicy(root=tmp_path))


def test_css_import_outside_root_rejected(assets):
    """CSS @import cannot load stylesheets outside of the root."""
    (assets.outside / 'evil.css').write_text('.x { fill: red; }')
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
        '<style>@import "../outside/evil.css";</style>'
        '<rect width="10" height="10" fill="red"/></svg>')
    (assets.root / 'doc.svg').write_text(svg)
    with pytest.raises(ResourceAccessError):
        svg2png(
            url=str(assets.root / 'doc.svg'),
            resource_policy=ResourcePolicy(root=assets.root))


def test_fragment_references_not_rejected(tmp_path):
    """#fragment references are internal, not external resource reads."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">
      <defs>
        <linearGradient id="grad">
          <stop offset="0" stop-color="red"/>
        </linearGradient>
        <rect id="shape" width="5" height="5" fill="url(#grad)"/>
      </defs>
      <use href="#shape" x="0" y="0"/>
      <use href="#shape" x="10" y="0"/>
    </svg>'''
    assert svg2png(
        bytestring=svg,
        resource_policy=ResourcePolicy(root=tmp_path)).startswith(PNG_MAGIC)


def test_error_keeps_resource_and_reason_without_secrets(tmp_path):
    """The exception keeps the URL and reason but leaks no credentials."""
    url = 'http://user:secretpassword@example.invalid/x.svg?token=abc123'
    with pytest.raises(ResourceAccessError) as exc_info:
        svg2png(url=url, resource_policy=ResourcePolicy(root=tmp_path))
    error = exc_info.value
    assert error.url == url
    assert error.reason
    message = str(error)
    assert 'example.invalid' in message
    assert 'secretpassword' not in message
    assert 'abc123' not in message


def test_xml_entity_protection_kept(tmp_path):
    """The safe-mode XML entity protection is not disabled by the policy."""
    with pytest.raises(EntitiesForbidden):
        svg2png(
            bytestring=XXE_SVG,
            resource_policy=ResourcePolicy(root=tmp_path))


def test_behavior_unchanged_without_policy(tmp_path):
    """Without a policy, the existing (safe) behavior is kept."""
    # External HTTP image: silently replaced in safe mode, no exception.
    svg = image_svg('http://example.invalid/x.png')
    assert svg2png(bytestring=svg).startswith(PNG_MAGIC)
    # data: URLs still work.
    svg = image_svg(
        'data:image/png;base64,' +
        base64.b64encode(png_bytes()).decode())
    assert svg2png(bytestring=svg).startswith(PNG_MAGIC)
