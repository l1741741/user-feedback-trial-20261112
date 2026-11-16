"""Tests for nested SVG ``image`` rendering isolation and size mapping.

These tests render real PNG output and check the actual pixels, so that
both definition isolation and coordinate mapping are verified against
what Cairo really paints and not only against the parsed tree.

"""

import io
import os

import pytest

from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface
from cairosvg.url import safe_fetch

FIXTURES = os.path.join(os.path.dirname(__file__), 'nested_svg_fixtures')


def local_fetcher(url, resource_type):
    """Trusted local-only URL fetcher used by the tests."""
    assert url.startswith('file://'), f'unexpected non-local URL: {url}'
    path = url[len('file://'):].split('#', 1)[0]
    with open(path, 'rb') as file_object:
        return file_object.read()


def render(bytestring=None, *, url=None, url_fetcher=local_fetcher):
    """Render an SVG document to a PIL RGBA image."""
    from PIL import Image

    tree = Tree(
        bytestring=bytestring, url=url, url_fetcher=url_fetcher)
    output = io.BytesIO()
    PNGSurface(tree, output, 96).finish()
    return Image.open(io.BytesIO(output.getvalue())).convert('RGBA')


def read_fixture(name):
    with open(os.path.join(FIXTURES, name), 'rb') as file_object:
        return file_object.read()


def assert_close(actual, expected, tolerance=8):
    assert abs(actual - expected) <= tolerance, (actual, expected)


def assert_color(pixel, rgba, tolerance=8):
    assert len(pixel) == 4
    for channel, expected in zip(pixel, rgba):
        assert_close(channel, expected, tolerance)


def test_same_id_definitions_do_not_pollute_each_other():
    """Two icons with same-id gradients/clipPaths keep their own colors."""
    image = render(url=os.path.join(FIXTURES, 'main.svg'))
    # Icon 1 defines a green gradient id="g", icon 2 an orange one.
    assert_color(image.getpixel((25, 25)), (0, 185, 0))
    assert_color(image.getpixel((75, 25)), (194, 101, 0))
    # The main document's own blue gradient id="g" is untouched.
    assert_color(image.getpixel((50, 70)), (0, 0, 255))
    # Same-id clipPath of the icons clips their rects but not the main doc.
    assert image.getpixel((2, 2)) == (0, 0, 0, 0)
    assert_color(image.getpixel((10, 60)), (0, 0, 255))


def test_duplicate_embedding_of_same_document():
    """The same embedded SVG is drawn twice at two independent positions."""
    main = b'''<svg xmlns="http://www.w3.org/2000/svg" width="120" height="60"
                 viewBox="0 0 120 60">
      <image x="5" y="10" width="40" height="40" href="icon1.svg"/>
      <image x="75" y="10" width="40" height="40" href="icon1.svg"/>
    </svg>'''
    image = render(
        main, url='file://' + os.path.join(FIXTURES, 'main.svg'))
    # Both copies paint the icon's green gradient.
    assert_color(image.getpixel((25, 30)), (0, 185, 0))
    assert_color(image.getpixel((95, 30)), (0, 185, 0))
    # Nothing is painted between the two copies.
    assert image.getpixel((60, 30)) == (0, 0, 0, 0)


def test_duplicate_embedding_preserves_aspect_ratio_both_times():
    """Reusing a cached embedded tree must not leak the meet/slice state."""
    main = b'''<svg xmlns="http://www.w3.org/2000/svg" width="100"
                 height="100" viewBox="0 0 100 100">
      <image x="10" y="5" width="80" height="40"
             href="viewbox_only.svg"/>
      <image x="10" y="55" width="80" height="40"
             href="viewbox_only.svg"/>
    </svg>'''
    image = render(
        main, url='file://' + os.path.join(FIXTURES, 'main.svg'))
    navy = (0, 0, 128)
    # Both copies letterbox the same way: centered navy square, empty
    # side bands in the first and the second image region.
    for center_y in (25, 75):
        assert_color(image.getpixel((50, center_y)), navy)
        assert image.getpixel((15, center_y)) == (0, 0, 0, 0)
        assert image.getpixel((85, center_y)) == (0, 0, 0, 0)


def test_relative_resources_resolve_per_document_source(tmp_path):
    """Relative hrefs of an embedded document use its own base URL."""
    sub_dir = tmp_path / 'sub'
    sub_dir.mkdir()
    icon = (sub_dir / 'local_icon.svg')
    icon.write_bytes(read_fixture('icon1.svg'))
    main = tmp_path / 'main.svg'
    main.write_bytes(b'''<svg xmlns="http://www.w3.org/2000/svg"
        width="50" height="50" viewBox="0 0 50 50">
      <image x="5" y="5" width="40" height="40" href="sub/local_icon.svg"/>
    </svg>''')
    # The main document has no local_icon.svg next to it: resolution must
    # follow the embedded document's own location.
    image = render(url=str(main))
    assert_color(image.getpixel((25, 25)), (0, 185, 0))


def test_relative_resource_of_embedded_svg_not_main(tmp_path):
    """A relative name matching another file in the main dir is not used."""
    sub_dir = tmp_path / 'sub'
    sub_dir.mkdir()
    nested = sub_dir / 'nested.svg'
    nested.write_bytes(b'''<svg xmlns="http://www.w3.org/2000/svg"
        width="40" height="40" viewBox="0 0 40 40">
      <image x="0" y="0" width="40" height="40" href="inner.svg"/>
    </svg>''')
    (sub_dir / 'inner.svg').write_bytes(
        b'''<svg xmlns="http://www.w3.org/2000/svg"
        width="40" height="40" viewBox="0 0 40 40">
      <rect width="40" height="40" fill="rgb(0,170,0)"/>
    </svg>''')
    # Same basename in the main directory, different (red) content: it
    # must not be picked up by the nested document.
    (tmp_path / 'inner.svg').write_bytes(
        b'''<svg xmlns="http://www.w3.org/2000/svg"
        width="40" height="40" viewBox="0 0 40 40">
      <rect width="40" height="40" fill="red"/>
    </svg>''')
    main = tmp_path / 'main.svg'
    main.write_bytes(b'''<svg xmlns="http://www.w3.org/2000/svg"
        width="50" height="50" viewBox="0 0 50 50">
      <image x="5" y="5" width="40" height="40" href="sub/nested.svg"/>
    </svg>''')
    image = render(url=str(main))
    assert_color(image.getpixel((25, 25)), (0, 170, 0))


def test_nested_viewbox_mapping_and_letterboxing():
    """A square viewBox in a wide region is centered, not stretched."""
    main = b'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"
                 viewBox="0 0 100 100">
      <image x="10" y="10" width="80" height="40" href="viewbox_only.svg"/>
    </svg>'''
    image = render(
        main, url='file://' + os.path.join(FIXTURES, 'main.svg'))
    navy = (0, 0, 128)
    # Centered 40x40 square inside the 80x40 region (x:30-70, y:10-50).
    assert_color(image.getpixel((50, 30)), navy)
    assert_color(image.getpixel((32, 30)), navy)
    assert_color(image.getpixel((68, 30)), navy)
    # Side bands are transparent: the viewBox was not stretched.
    assert image.getpixel((15, 30)) == (0, 0, 0, 0)
    assert image.getpixel((85, 30)) == (0, 0, 0, 0)
    # Top and bottom of the square are painted, bands above/below empty.
    assert_color(image.getpixel((50, 12)), navy)
    assert_color(image.getpixel((50, 48)), navy)
    assert image.getpixel((50, 8)) == (0, 0, 0, 0)
    assert image.getpixel((50, 52)) == (0, 0, 0, 0)


def test_nested_svg_without_intrinsic_size_fills_region():
    """A viewBox-only embedded SVG deterministically fills the region."""
    main = b'''<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60"
                 viewBox="0 0 60 60">
      <image x="5" y="5" width="50" height="50" href="no_size.svg"/>
    </svg>'''
    image = render(
        main, url='file://' + os.path.join(FIXTURES, 'main.svg'))
    assert_color(image.getpixel((30, 30)), (0, 128, 128))
    # Region boundaries are respected.
    assert image.getpixel((2, 2)) == (0, 0, 0, 0)
    assert image.getpixel((57, 57)) == (0, 0, 0, 0)


def test_nested_preserve_aspect_ratio_slice_crops():
    """preserveAspectRatio="xMidYMid slice" fills the region and crops."""
    main = b'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"
                 viewBox="0 0 100 100">
      <image x="10" y="10" width="80" height="40"
             href="slice_icon.svg"/>
    </svg>'''
    image = render(
        main, url='file://' + os.path.join(FIXTURES, 'main.svg'))
    # Slice scales the square viewBox to cover the wide region: the
    # region is fully painted and the overflow is cropped by the clip.
    assert_color(image.getpixel((15, 15)), (0, 0, 128))
    assert_color(image.getpixel((85, 45)), (0, 0, 128))
    assert_color(image.getpixel((50, 30)), (0, 0, 128))
    # Nothing escapes the image region.
    assert image.getpixel((5, 5)) == (0, 0, 0, 0)
    assert image.getpixel((95, 55)) == (0, 0, 0, 0)


def test_gzip_compressed_embedded_svg():
    """A gzip-compressed embedded SVG document is drawn like a plain one."""
    import gzip

    icon = gzip.compress(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        b'<rect width="20" height="20" fill="rgb(40,50,60)"/></svg>')

    def fetcher(url, resource_type):
        if url.endswith('icon.svgz'):
            return icon
        return local_fetcher(url, resource_type)

    main = b'''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">
      <image width="20" height="20" href="icon.svgz"/>
    </svg>'''
    image = render(
        main, url='file://' + os.path.join(FIXTURES, 'main.svg'),
        url_fetcher=fetcher)
    assert_color(image.getpixel((10, 10)), (40, 50, 60))


def test_nested_preserve_aspect_ratio_none_stretches():
    """preserveAspectRatio="none" of the embedded doc stretches its viewBox."""
    main = b'''<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"
                 viewBox="0 0 100 100">
      <image x="10" y="10" width="80" height="40"
             href="stretch_icon.svg"/>
    </svg>'''
    image = render(
        main, url='file://' + os.path.join(FIXTURES, 'main.svg'))
    # The whole region is filled with the stretched document.
    assert_color(image.getpixel((15, 15)), (0, 0, 128))
    assert_color(image.getpixel((85, 45)), (0, 0, 128))
    assert_color(image.getpixel((50, 30)), (0, 0, 128))


def test_embedded_image_opacity_applies_to_document():
    """The image opacity alpha-composites the whole embedded document."""
    main = b'''<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"
                 viewBox="0 0 40 40">
      <image x="0" y="0" width="40" height="40" opacity="0.25"
             href="opaque_red.svg"/>
    </svg>'''
    image = render(
        main, url='file://' + os.path.join(FIXTURES, 'main.svg'))
    assert_color(image.getpixel((20, 20)), (255, 0, 0, 64), tolerance=3)


def test_embedded_transform_is_applied():
    """A transform on the image node maps the embedded document correctly."""
    main = b'''<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"
                 viewBox="0 0 80 80">
      <g transform="translate(10, 10)">
        <image x="0" y="0" width="40" height="40"
               transform="rotate(90 20 20)" href="corner_marks.svg"/>
      </g>
    </svg>'''
    image = render(
        main, url='file://' + os.path.join(FIXTURES, 'main.svg'))
    # The icon paints a red square in its top-left corner; after a 90deg
    # rotation it must be found in the top-right corner of the image region.
    assert_color(image.getpixel((45, 15)), (255, 0, 0))
    # The blue bottom-right corner moves to the bottom-left.
    assert_color(image.getpixel((15, 45)), (0, 0, 255))
    # The other two corners stay transparent.
    assert image.getpixel((15, 15)) == (0, 0, 0, 0)
    assert image.getpixel((45, 45)) == (0, 0, 0, 0)


def test_broken_embedded_svg_keeps_siblings():
    """A malformed embedded SVG is skipped without killing sibling drawing."""
    main = b'''<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"
                 viewBox="0 0 80 80">
      <image x="5" y="5" width="40" height="40" href="broken.svg"/>
      <rect x="5" y="50" width="70" height="25" fill="red"/>
    </svg>'''
    image = render(
        main, url='file://' + os.path.join(FIXTURES, 'main.svg'))
    # The broken image leaves no paint...
    assert image.getpixel((15, 15)) == (0, 0, 0, 0)
    # ...but the sibling rect is still rendered.
    assert_color(image.getpixel((10, 60)), (255, 0, 0))


def test_missing_resource_keeps_siblings(tmp_path):
    """A missing embedded resource is skipped, siblings still draw."""
    main = tmp_path / 'main.svg'
    main.write_bytes(b'''<svg xmlns="http://www.w3.org/2000/svg"
        width="80" height="80" viewBox="0 0 80 80">
      <image x="5" y="5" width="40" height="40" href="missing.svg"/>
      <rect x="5" y="50" width="70" height="25" fill="blue"/>
    </svg>''')

    def fetcher(url, resource_type):
        if url.endswith('missing.svg'):
            raise FileNotFoundError(url)
        return local_fetcher(url, resource_type)

    image = render(url=str(main), url_fetcher=fetcher)
    assert image.getpixel((15, 15)) == (0, 0, 0, 0)
    assert_color(image.getpixel((10, 60)), (0, 0, 255))


def test_safe_fetch_policy_is_not_relaxed(tmp_path):
    """Without a trusted fetcher, external embedded resources stay blocked."""
    main = tmp_path / 'main.svg'
    icon = tmp_path / 'icon.svg'
    icon.write_bytes(b'''<svg xmlns="http://www.w3.org/2000/svg"
        width="10" height="10"><rect width="10" height="10" fill="red"/>
    </svg>''')
    main.write_bytes(b'''<svg xmlns="http://www.w3.org/2000/svg"
        width="20" height="20" viewBox="0 0 20 20">
      <image x="0" y="0" width="20" height="20"
             href="file://ICON_PATH"/>
    </svg>'''.replace(b'ICON_PATH', str(icon).encode()))
    # No url_fetcher and not unsafe: safe_fetch must return the empty
    # placeholder instead of reading the file.
    tree = Tree(url=str(main))
    output = io.BytesIO()
    PNGSurface(tree, output, 96).finish()
    from PIL import Image
    image = Image.open(io.BytesIO(output.getvalue())).convert('RGBA')
    assert image.getpixel((10, 10)) == (0, 0, 0, 0)
    # The policy function itself is unchanged and still only allows data:.
    assert safe_fetch('file:///etc/passwd', 'image/svg+xml') == (
        b'<svg width="1" height="1"></svg>')


def test_data_url_embedded_svg_remains_supported():
    """data: URL embedded SVG still renders under the default policy."""
    import base64

    icon = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        '<rect width="20" height="20" fill="rgb(10,20,30)"/></svg>')
    data_url = 'data:image/svg+xml;base64,' + base64.b64encode(
        icon.encode()).decode()
    main = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        f'<image width="20" height="20" href="{data_url}"/></svg>').encode()
    image = render(
        main, url=None,
        url_fetcher=lambda url, resource_type: safe_fetch(url, resource_type))
    assert_color(image.getpixel((10, 10)), (10, 20, 30))
