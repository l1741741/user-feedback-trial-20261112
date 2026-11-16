"""
Rendering state isolation regression tests.

These tests exercise real Cairo rendering (no mocked ``save``/``restore``)
and compare actual pixels across call and node boundaries. They cover the
failure modes of shared drawing state:

* a special layer (transform / clip / mask / filter / opacity group /
  non-default operator / pattern source) followed by a plain sibling must
  render byte-identically to the plain sibling rendered alone;
* a rendering that raises mid-draw must propagate the *original* error and
  leave the next independent entry-point call producing the reference
  pixels;
* empty layers and transparent groups must not leak state either;
* temporary Cairo surfaces created for masks/patterns/nested SVG must be
  released, and repeated exports must stay identical.

"""

import io
import weakref

import cairocffi as cairo
import pytest
from PIL import Image

from . import svg2png
from .parser import Tree
from .surface import PNGSurface

# A small, valid PNG header followed by a truncated stream: Cairo's libpng
# rejects it with a real CAIRO_STATUS_PNG_ERROR while drawing, i.e. after the
# node's save/group frame has been opened.
BROKEN_PNG_DATA_URI = (
    'data:image/png;base64,'
    'iVBORw0KGgoAAAANSUhEUgAAAAUAAAAFCAAAAABad+Rb')

WIDTH = HEIGHT = 120
SVG_OPEN = '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">'
SVG_HEADER_BYTES = SVG_OPEN.encode()


def render(svg, **kwargs):
    """Render ``svg`` and return the decoded RGBA pixel matrix."""
    png = svg2png(svg, **kwargs) if 'url_fetcher' not in kwargs else \
        PNGSurface.convert(bytestring=svg, **kwargs)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def pixels(image):
    """Raw RGBA bytes, stable across Pillow versions."""
    return image.tobytes()


def non_transparent(image):
    return image.getchannel('A').getextrema()[1] > 0


def has_transparent_pixel(image):
    return image.getchannel('A').getextrema()[0] == 0


def assert_pixels_equal(actual, expected, label):
    """Two renders must be pixel-identical, with a readable diff on failure."""
    assert actual.size == expected.size, (
        f'{label}: size {actual.size} != {expected.size}')
    actual_bytes = pixels(actual)
    expected_bytes = pixels(expected)
    if actual_bytes != expected_bytes:
        diff_count = sum(
            1 for a, b in zip(actual_bytes, expected_bytes) if a != b)
        pytest.fail(
            f'{label}: {diff_count} differing RGBA byte values '
            f'({diff_count // 4} pixels)')


def red_square_svg(x=70, y=70, size=40, color='red'):
    """The plain baseline document: no special effect at all."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg"
        width="{WIDTH}" height="{HEIGHT}">
      <rect x="{x}" y="{y}" width="{size}" height="{size}" fill="{color}"/>
    </svg>'''.encode()


# ---------------------------------------------------------------------------
# Real "legend" documents, one special effect each, followed by the baseline.
# ---------------------------------------------------------------------------

def legend_transform():
    # translate + rotate + scale on a semi-transparent group
    return SVG_HEADER_BYTES + b'''
      <g transform="translate(10,10) rotate(25) scale(1.2)" opacity="0.4">
        <rect x="0" y="0" width="30" height="30" fill="blue"/>
      </g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''


def legend_clip_path():
    return SVG_HEADER_BYTES + b'''
      <defs><clipPath id="clip">
        <circle cx="25" cy="25" r="20"/>
      </clipPath></defs>
      <g clip-path="url(#clip)" transform="translate(4,4)">
        <rect x="0" y="0" width="50" height="50" fill="blue"/>
      </g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''


def legend_clip_rect():
    # The CSS-style clip attribute takes the save/translate/clip branch.
    return SVG_HEADER_BYTES + b'''
      <g clip="rect(2px,48px,48px,2px)" x="0" y="0"
         width="60" height="60" transform="translate(3,3)">
        <rect x="0" y="0" width="50" height="50" fill="blue"/>
      </g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''


def legend_mask():
    return SVG_HEADER_BYTES + b'''
      <defs><mask id="m">
        <rect x="0" y="0" width="50" height="50" fill="white"/>
        <circle cx="25" cy="25" r="12" fill="black"/>
      </mask></defs>
      <g mask="url(#m)" transform="translate(2,2)">
        <rect x="0" y="0" width="50" height="50" fill="blue"/>
      </g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''


def legend_filter():
    # feOffset moves the group, feBlend installs a non-default operator.
    return SVG_HEADER_BYTES + b'''
      <defs><filter id="f" x="-20%" y="-20%" width="140%" height="140%">
        <feOffset dx="6" dy="4"/>
        <feBlend mode="multiply" in="SourceGraphic"/>
      </filter></defs>
      <g filter="url(#f)">
        <rect x="5" y="5" width="30" height="30" fill="blue"/>
      </g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''


def legend_opacity_group():
    # A pushed transparency group with nested children.
    return SVG_HEADER_BYTES + b'''
      <g opacity="0.35">
        <rect x="5" y="5" width="25" height="25" fill="blue"/>
        <rect x="20" y="20" width="25" height="25" fill="green"/>
      </g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''


def legend_pattern_source():
    return SVG_HEADER_BYTES + b'''
      <defs><pattern id="p" width="8" height="8" patternUnits="userSpaceOnUse">
        <rect width="8" height="8" fill="blue"/>
        <rect width="4" height="4" fill="white"/>
      </pattern></defs>
      <rect x="2" y="2" width="50" height="50" fill="url(#p)"
            transform="rotate(12 27 27)"/>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''


def legend_gradient_source():
    return SVG_HEADER_BYTES + b'''
      <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="blue"/>
        <stop offset="1" stop-color="green"/>
      </linearGradient></defs>
      <g transform="translate(4,4)">
        <rect x="0" y="0" width="50" height="50" fill="url(#g)"/>
      </g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''


def legend_text():
    # Text exercises the per-glyph save/restore frames in text.py.
    return SVG_HEADER_BYTES + b'''
      <g transform="translate(4,20) rotate(-8)" opacity="0.8">
        <text x="0" y="20" font-family="sans-serif" font-size="18"
              fill="blue">Abc</text>
      </g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''


LEGENDS = [
    'transform', 'clip_path', 'clip_rect', 'mask', 'filter',
    'opacity_group', 'pattern_source', 'gradient_source', 'text',
]


def legend_document(name):
    return globals()[f'legend_{name}']()


@pytest.mark.parametrize('legend', LEGENDS)
def test_special_layer_does_not_leak_into_sibling(legend):
    """Sibling pixels equal the baseline rendered on its own."""
    baseline = render(red_square_svg())
    combined = render(legend_document(legend))
    assert_pixels_equal(
        combined.crop((70, 70, 110, 110)), baseline.crop((70, 70, 110, 110)),
        f'{legend}: baseline sibling region')


@pytest.mark.parametrize('legend', LEGENDS)
def test_special_layer_keeps_own_pixels(legend):
    """The special region must keep rendering (no swallowed art)."""
    combined = render(legend_document(legend))
    special_region = combined.crop((0, 0, 60, 60))
    assert non_transparent(special_region), (
        f'{legend}: special-effect region is fully transparent')


def test_empty_layer_leaves_no_state():
    """Zero-sized / display:none / empty groups sit between real layers."""
    baseline = render(red_square_svg())
    document = SVG_HEADER_BYTES + b'''
      <g transform="translate(30,30) rotate(45)" opacity="0.2">
        <rect width="0" height="40" fill="blue"/>
      </g>
      <g transform="translate(5,5)" display="none">
        <rect width="40" height="40" fill="blue"/>
      </g>
      <g></g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''
    assert_pixels_equal(
        render(document), baseline, 'empty layers vs baseline')


def test_transparent_group_does_not_flatten_sibling():
    """The opacity group only changes its own subtree, not the sibling."""
    baseline = render(red_square_svg())
    document = legend_opacity_group()
    combined = render(document)
    assert_pixels_equal(
        combined.crop((70, 70, 110, 110)),
        baseline.crop((70, 70, 110, 110)),
        'opacity group sibling region')


# ---------------------------------------------------------------------------
# Mid-draw exceptions: real error propagation + post-failure re-entry.
# ---------------------------------------------------------------------------

def failing_group_document():
    return f'''{SVG_OPEN}
      <g opacity="0.5" transform="translate(8,8) rotate(20)">
        <rect width="40" height="40" fill="blue"/>
        <image href="{BROKEN_PNG_DATA_URI}"
               x="2" y="2" width="20" height="20"/>
      </g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''.encode()


def failing_clip_document():
    return f'''{SVG_OPEN}
      <defs><clipPath id="clip">
        <image href="{BROKEN_PNG_DATA_URI}" width="20" height="20"/>
      </clipPath></defs>
      <g clip-path="url(#clip)" transform="translate(6,6)">
        <rect width="50" height="50" fill="blue"/>
      </g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''.encode()


def failing_fetcher_document():
    return SVG_HEADER_BYTES + b'''
      <g opacity="0.5">
        <image href="http://network.unreachable.example/x.png"
               width="20" height="20"/>
      </g>
    </svg>'''


class NetworkDisabled(Exception):
    """Stand-in for a batch export worker's fetch failure."""


def refusing_fetcher(url, resource_type):
    raise NetworkDisabled(url)


@pytest.mark.parametrize('document', [
    pytest.param(failing_group_document(), id='opacity-group'),
    pytest.param(failing_clip_document(), id='clip-path'),
])
def test_original_error_propagates(document):
    """The real Cairo error must not be replaced by INVALID_RESTORE."""
    with pytest.raises(cairo.CairoError, match='PNG'):
        svg2png(document)


def test_fetcher_error_propagates_unchanged():
    """Application-level exceptions keep their own type through rendering."""
    with pytest.raises(NetworkDisabled):
        PNGSurface.convert(
            bytestring=failing_fetcher_document(),
            url_fetcher=refusing_fetcher)


@pytest.mark.parametrize('document', [
    pytest.param(failing_group_document(), id='opacity-group'),
    pytest.param(failing_clip_document(), id='clip-path'),
])
def test_render_after_failure_matches_baseline(document):
    """A fresh entry-point call after a failure renders reference pixels."""
    baseline = render(red_square_svg())
    with pytest.raises(cairo.CairoError):
        svg2png(document)
    # Reuse the same process / module-level state, as a batch worker would.
    assert_pixels_equal(
        render(red_square_svg()), baseline, 're-entry after failure')


def test_fetcher_failure_then_baseline():
    baseline = render(red_square_svg())
    with pytest.raises(NetworkDisabled):
        PNGSurface.convert(
            bytestring=failing_fetcher_document(),
            url_fetcher=refusing_fetcher)
    assert_pixels_equal(
        render(red_square_svg()), baseline,
        're-entry after fetcher failure')


def test_special_layer_then_failure_then_baseline():
    """Success with effects, then a failure, then a plain baseline render."""
    baseline = render(red_square_svg())
    render(legend_transform())
    render(legend_mask())
    with pytest.raises(cairo.CairoError, match='PNG'):
        svg2png(failing_group_document())
    assert_pixels_equal(
        render(red_square_svg()), baseline,
        'baseline after success, failure, success')


def test_clip_path_definition_restored_after_failure():
    """A shared clipPath node keeps its tag and stroke flag after a failure."""
    document = failing_clip_document()
    with pytest.raises(cairo.CairoError):
        svg2png(document)
    # The second document reuses the same kind of shared definition; a stale
    # 'g' tag on the clip path would silently drop the clip.
    clipped = SVG_HEADER_BYTES + b'''
      <defs><clipPath id="clip">
        <rect x="10" y="10" width="40" height="40"/>
      </clipPath></defs>
      <rect x="0" y="0" width="120" height="120" fill="red"
            clip-path="url(#clip)"/>
    </svg>'''
    image = render(clipped)
    # Inside the clip: red. Outside (corner): transparent.
    assert image.getpixel((30, 30)) == (255, 0, 0, 255)
    assert image.getpixel((2, 2)) == (0, 0, 0, 0)


def _capture_failed_surface(document):
    """Build a surface from ``document``, failing in ``draw``."""
    from .parser import Tree
    tree = Tree(bytestring=document)
    captured = {}
    original_init = PNGSurface.__init__

    def tracking_init(self, *args, **kwargs):
        captured['surface'] = self
        return original_init(self, *args, **kwargs)

    PNGSurface.__init__ = tracking_init
    try:
        with pytest.raises(cairo.CairoError, match='PNG'):
            PNGSurface(tree, io.BytesIO(), 96)
    finally:
        PNGSurface.__init__ = original_init
    return captured['surface']


def test_failed_clip_path_node_is_reset():
    """The clipPath node must not be left tagged 'g' after a failed clip."""
    surface = _capture_failed_surface(failing_clip_document())
    # The constructor's failure path releases the Cairo surface, while the
    # Python object stays alive for inspection.
    assert surface.cairo is None
    clip_node = next(iter(surface.paths.values()))
    assert clip_node.tag == 'clipPath'
    # The flag flipped for recursive clip drawing must be back to normal.
    assert surface.stroke_and_fill is True


def test_failed_mask_node_is_reset_and_surface_closed():
    """A mask node keeps its tag and its temporary surface is released."""
    document = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">'
        b'<defs><mask id="m">'
        b'<rect width="50" height="50" fill="white"/>'
        b'<image href="data:image/png;base64,'
        b'iVBORw0KGgoAAAANSUhEUgAAAAUAAAAFCAAAAABad+Rb" '
        b'width="10" height="10"/>'
        b'</mask></defs>'
        b'<g mask="url(#m)">'
        b'<rect width="60" height="60" fill="blue"/></g></svg>')
    surface = _capture_failed_surface(document)
    assert surface.cairo is None
    mask_node = next(iter(surface.masks.values()))
    assert mask_node.tag == 'mask'


def test_surface_closed_even_when_draw_raises_before_any_frame():
    """A failure very early in construction still closes the Cairo surface."""
    from .parser import Tree

    class EarlyFailure(Exception):
        pass

    tree = Tree(bytestring=red_square_svg())
    captured = {}
    original_init = PNGSurface.__init__

    def tracking_init(self, *args, **kwargs):
        captured['surface'] = self
        return original_init(self, *args, **kwargs)

    # Fail inside the very first draw entry, before any node frame opens.
    import cairosvg.surface as surface_module
    original_parse = surface_module.parse_all_defs

    def failing_parse(surface, node):
        raise EarlyFailure

    PNGSurface.__init__ = tracking_init
    surface_module.parse_all_defs = failing_parse
    try:
        with pytest.raises(EarlyFailure):
            PNGSurface(tree, io.BytesIO(), 96)
    finally:
        PNGSurface.__init__ = original_init
        surface_module.parse_all_defs = original_parse
    assert captured['surface'].cairo is None


def test_mask_definition_restored_after_failure(tmp_path):
    """A mask node mutated by a failed paint_mask still works afterwards."""
    inner = tmp_path / 'inner.svg'
    inner.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
        '<image href="data:image/png;base64,'
        'iVBORw0KGgoAAAANSUhEUgAAAAUAAAAFCAAAAABad+Rb'
        '" width="20" height="20"/>'
        '</svg>')
    document = f'''{SVG_OPEN}
      <defs><mask id="m">
        <rect x="0" y="0" width="50" height="50" fill="white"/>
      </mask></defs>
      <g mask="url(#m)">
        <image href="file://{inner}" width="40" height="40"/>
      </g>
    </svg>'''.encode()
    with pytest.raises(cairo.CairoError):
        svg2png(document, unsafe=True)
    # Re-render a valid mask document; a stale tag='g' would break masking.
    valid = render(legend_mask())
    assert non_transparent(valid.crop((0, 0, 50, 50)))
    # The mask punches a transparent hole inside the masked region.
    assert has_transparent_pixel(valid.crop((0, 0, 50, 50)))


# ---------------------------------------------------------------------------
# Resource release and repeated-export regressions.
# ---------------------------------------------------------------------------

def test_failed_surface_releases_cairo_surface():
    """No live Surface survives a failed construction."""
    from .parser import Tree

    tree = Tree(
        bytestring=failing_fetcher_document(), url_fetcher=refusing_fetcher)
    refs = []
    original_init = PNGSurface.__init__

    def tracking_init(self, *args, **kwargs):
        refs.append(weakref.ref(self))
        return original_init(self, *args, **kwargs)

    PNGSurface.__init__ = tracking_init
    try:
        with pytest.raises(NetworkDisabled):
            PNGSurface(tree, io.BytesIO(), 96)
    finally:
        PNGSurface.__init__ = original_init

    assert refs, 'surface was never constructed'
    # The failure path must have finished the Cairo surface; once Python drops
    # the instance nothing should keep it (nor its temporary surfaces) alive.
    import gc
    gc.collect()
    assert all(ref() is None for ref in refs), (
        'failed Surface instance was not released')


def test_repeated_exports_are_identical():
    """Repeated batch exports of every legend stay byte-stable."""
    baseline = render(red_square_svg())
    for _ in range(5):
        for legend in LEGENDS:
            render(legend_document(legend))
        assert_pixels_equal(
            render(red_square_svg()), baseline,
            'baseline after repeated legend exports')
    # And the legends themselves remain deterministic.
    for legend in LEGENDS:
        first = pixels(render(legend_document(legend)))
        second = pixels(render(legend_document(legend)))
        assert first == second, f'{legend}: non-deterministic repeat export'


def test_nested_svg_failure_releases_and_keeps_error(tmp_path):
    """Temporary surfaces of nested SVG images are finished on failure."""
    inner = tmp_path / 'inner.svg'
    inner.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
        f'<g opacity="0.5"><image href="{BROKEN_PNG_DATA_URI}" '
        'width="20" height="20"/></g></svg>')
    outer = f'''{SVG_OPEN}
      <g transform="translate(5,5)">
        <image href="file://{inner}" width="60" height="60"/>
      </g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''.encode()
    with pytest.raises(cairo.CairoError, match='PNG'):
        svg2png(outer, unsafe=True)
    baseline = render(red_square_svg())
    assert_pixels_equal(
        render(red_square_svg()), baseline,
        'baseline after nested SVG failure')


def test_output_not_corrupted_by_failure(tmp_path):
    """A failed file export must not leave a half-written PNG behind."""
    output = tmp_path / 'broken.png'
    with pytest.raises(cairo.CairoError):
        svg2png(failing_group_document(), write_to=str(output))
    if output.exists():
        data = output.read_bytes()
        assert not data.startswith(b'\211PNG') or len(data) == 0, (
            'a failed render left PNG bytes in the output file')


def test_finish_is_idempotent():
    """Calling finish() twice must not raise."""
    from .parser import Tree
    tree = Tree(bytestring=red_square_svg())
    surface = PNGSurface(tree, None, 96)
    surface.finish()
    surface.finish()


def test_pattern_temporary_surface_lives_until_finish():
    """Pattern tiles must stay alive for the paint, then be released."""
    import cairosvg.surface as surface_module

    document = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="60" height="60">'
        b'<defs><pattern id="p" width="8" height="8" '
        b'patternUnits="userSpaceOnUse">'
        b'<rect width="8" height="8" fill="blue"/>'
        b'</pattern></defs>'
        b'<rect width="50" height="50" fill="url(#p)"/></svg>')
    tree = Tree(bytestring=document)
    nested_refs = []
    original_init = surface_module.SVGSurface.__init__

    def tracking_init(self, *args, **kwargs):
        nested_refs.append(weakref.ref(self))
        return original_init(self, *args, **kwargs)

    surface_module.SVGSurface.__init__ = tracking_init
    try:
        surface = PNGSurface(tree, None, 96)
        # The pattern must still be readable when the main surface paints.
        assert nested_refs and nested_refs[0]() is not None
        surface.finish()
    finally:
        surface_module.SVGSurface.__init__ = original_init

    import gc
    gc.collect()
    assert all(ref() is None for ref in nested_refs)


def test_repeated_mask_and_pattern_exports_stay_painted():
    """Repeated use of mask/pattern defs must not degrade into blank output."""
    document = SVG_HEADER_BYTES + b'''
      <defs>
        <mask id="m"><rect width="50" height="50" fill="white"/>
          <circle cx="25" cy="25" r="12" fill="black"/></mask>
        <pattern id="p" width="8" height="8" patternUnits="userSpaceOnUse">
          <rect width="8" height="8" fill="blue"/>
          <rect width="4" height="4" fill="white"/>
        </pattern>
      </defs>
      <g mask="url(#m)">
        <rect width="50" height="50" fill="url(#p)"/>
      </g>
      <rect x="70" y="70" width="40" height="40" fill="red"/>
    </svg>'''
    first = pixels(render(document))
    for _ in range(8):
        assert pixels(render(document)) == first
    rendered = render(document)
    assert non_transparent(rendered.crop((0, 0, 50, 50)))


def test_use_reference_counter_unwinds_on_failure():
    """A failing <use> branch must give back its reference slot."""
    from .parser import Tree

    # The <use> resolves then its referenced subtree fails while drawing.
    document = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
        b'<defs><g id="target">'
        b'<image href="data:image/png;base64,'
        b'iVBORw0KGgoAAAANSUhEUgAAAAUAAAAFCAAAAABad+Rb" '
        b'width="10" height="10"/></g></defs>'
        b'<use href="#target"/></svg>')
    tree = Tree(bytestring=document)
    captured = {}
    original_init = PNGSurface.__init__

    def tracking_init(self, *args, **kwargs):
        captured['surface'] = self
        return original_init(self, *args, **kwargs)

    PNGSurface.__init__ = tracking_init
    try:
        with pytest.raises(cairo.CairoError, match='PNG'):
            PNGSurface(tree, io.BytesIO(), 96)
    finally:
        PNGSurface.__init__ = original_init
    # The failing use() incremented the counter once and must have unwound it.
    assert captured['surface'].reference_count == 0
