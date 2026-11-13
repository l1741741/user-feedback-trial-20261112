"""
Tests for the safety and isolation of ``<use>`` reference rendering.

These tests render small, locally generated SVG documents (no external
material is ever downloaded) and probe individual pixels chosen away from
antialiased edges, so exact RGBA comparisons are deterministic across Cairo
versions.

They cover:

* the same ``symbol``/``g`` referenced at different positions and sizes,
* rendering order independence for repeated references,
* nested references,
* cyclic reference detection,
* the 100 000 expanded-reference boundary and the 128 level depth limit,
* clean state (context and node attributes) after a failed or filtered
  reference so that sibling elements still draw correctly,
* the external URL policy (``unsafe`` / ``url_fetcher`` / ``safe_fetch``),
* identical output on repeated renders,
* and the guarantee that neither the parsed ``Tree`` nor the underlying
  ElementTree XML is rewritten while rendering.

"""

import base64
import io

import pytest

from . import svg2png
from . import defs
from .parser import Tree
from .surface import PNGSurface

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (255, 0, 0)
GREEN = (0, 128, 0)
BLUE = (0, 0, 255)
ORANGE = (255, 165, 0)
PURPLE = (128, 0, 128)

SVG_NS = 'http://www.w3.org/2000/svg'


def render(source, **kwargs):
    """Render an SVG string/bytes and return the PNG bytes."""
    if isinstance(source, str):
        source = source.encode()
    return svg2png(bytestring=source, **kwargs)


def pixels(source, **kwargs):
    """Render ``source`` and return a PIL pixel accessor."""
    from PIL import Image
    image = Image.open(io.BytesIO(render(source, **kwargs))).convert('RGB')
    return image.load()


def assert_pixels(accessor, expected):
    """Assert that each ``(x, y): color`` probe matches exactly."""
    for (x, y), color in expected.items():
        assert accessor[x, y] == color, (
            f'pixel ({x}, {y}) = {accessor[x, y]}, expected {color}')


def surface_render(tree):
    """Render an already parsed ``Tree`` and return the PNG bytes."""
    output = io.BytesIO()
    PNGSurface(tree, output, 96).finish()
    return output.getvalue()


# ---------------------------------------------------------------------------
# Geometry and override semantics
# ---------------------------------------------------------------------------

def test_same_symbol_different_positions_and_sizes():
    """Two references to one symbol paint independent rectangles at their
    own x/y and scale their viewBox to their own width/height."""
    svg = f'''<svg xmlns="{SVG_NS}" width="120" height="120">
      <defs><symbol id="s" viewBox="0 0 10 10">
        <rect width="10" height="10" fill="red"/>
      </symbol></defs>
      <rect width="120" height="120" fill="white"/>
      <use href="#s" x="10" y="10" width="20" height="20"/>
      <use href="#s" x="50" y="60" width="40" height="30"/>
    </svg>'''
    assert_pixels(pixels(svg), {
        (20, 20): RED,      # center of the first 20x20 instance
        (11, 11): RED,
        (5, 5): WHITE,      # outside the first instance
        (70, 75): RED,      # center of the second 40x30 instance
        (40, 40): WHITE,    # gap between instances
        (95, 95): WHITE,    # outside the second instance
    })


def test_use_of_group_with_xy_offset():
    """For non svg/symbol targets x/y only translate the instance."""
    svg = f'''<svg xmlns="{SVG_NS}" width="60" height="40">
      <defs><g id="g"><rect width="10" height="10" fill="blue"/></g></defs>
      <rect width="60" height="40" fill="white"/>
      <use href="#g" x="20" y="15"/>
    </svg>'''
    assert_pixels(pixels(svg), {
        (25, 20): BLUE,
        (19, 19): WHITE,
        (31, 26): WHITE,
    })


def test_preserve_aspect_ratio_per_reference():
    """preserveAspectRatio of the symbol keeps its letterboxing rule for
    every instance independently."""
    svg = f'''<svg xmlns="{SVG_NS}" width="40" height="40">
      <defs><symbol id="s" viewBox="0 0 10 5"
             preserveAspectRatio="xMidYMid meet">
        <rect width="10" height="5" fill="red"/>
      </symbol></defs>
      <rect width="40" height="40" fill="white"/>
      <use href="#s" x="0" y="0" width="20" height="20"/>
    </svg>'''
    # scale 2 -> the 10x5 viewBox becomes a 20x10 rect centered vertically
    assert_pixels(pixels(svg), {
        (10, 10): RED,
        (10, 2): WHITE,    # letterbox band above
        (10, 18): WHITE,   # letterbox band below
    })


def test_fill_stroke_and_opacity_overrides():
    """fill/stroke set on <use> reach the cloned shadow content; opacity on
    <use> still groups the painted content."""
    svg = f'''<svg xmlns="{SVG_NS}" width="80" height="20">
      <defs><g id="g"><circle cx="10" cy="10" r="8"/></g></defs>
      <rect width="80" height="20" fill="white"/>
      <use href="#g" x="0" fill="blue"/>
      <use href="#g" x="20" fill="green" stroke="black" stroke-width="2"/>
      <use href="#g" x="40" fill="red"/>
    </svg>'''
    assert_pixels(pixels(svg), {
        (10, 10): BLUE,
        (30, 10): GREEN,
        (50, 10): RED,
        (75, 10): WHITE,
    })

    # opacity < 1 on a <use> with (cloned) children creates an intermediate
    # group, so black is blended onto the white background.
    svg_opacity = f'''<svg xmlns="{SVG_NS}" width="20" height="20">
      <defs><g id="g"><rect width="20" height="20" fill="black"/></g></defs>
      <rect width="20" height="20" fill="white"/>
      <use href="#g" opacity="0.5"/>
    </svg>'''
    # 50% black over white == 128 (Cairo rounds to 127/128).
    assert pixels(svg_opacity)[10, 10] in ((127, 127, 127), (128, 128, 128))


def test_mask_override_on_use():
    """A mask referenced by <use> clips the shadow content only."""
    svg = f'''<svg xmlns="{SVG_NS}" width="40" height="20">
      <defs>
        <g id="g"><rect width="20" height="20" fill="red"/></g>
        <mask id="m"><rect x="0" y="0" width="10" height="20" fill="white"/>
        </mask>
      </defs>
      <rect width="40" height="20" fill="white"/>
      <use href="#g" mask="url(#m)"/>
    </svg>'''
    assert_pixels(pixels(svg), {
        (5, 10): RED,
        (15, 10): WHITE,
    })


def test_nested_references():
    """A symbol may reference another symbol; positions and overrides
    compose through the nesting."""
    svg = f'''<svg xmlns="{SVG_NS}" width="80" height="40">
      <defs>
        <symbol id="inner" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </symbol>
        <symbol id="outer" viewBox="0 0 20 20">
          <rect width="20" height="20" fill="white"/>
          <use href="#inner" x="5" y="5" width="10" height="10"/>
        </symbol>
      </defs>
      <use href="#outer" x="10" y="0" width="40" height="40"/>
    </svg>'''
    # outer scales 2x: its inner use at (5,5) size 10 -> device (20,10)
    # size 20, inside an 40x40 viewport starting at x=10.
    assert_pixels(pixels(svg), {
        (30, 20): RED,      # center of the nested instance
        (15, 5): WHITE,     # outer background
        (5, 5): BLACK,      # transparent area outside the viewport
    })


# ---------------------------------------------------------------------------
# Order independence and repeated rendering
# ---------------------------------------------------------------------------

def test_rendering_order_independence():
    """Swapping two references must only swap where their instances paint."""
    template = '''<svg xmlns="{ns}" width="200" height="100">
      <defs><symbol id="s" viewBox="0 0 10 10">
        <rect width="10" height="10" fill="red"/>
      </symbol></defs>
      {uses}
    </svg>'''
    ab = template.format(
        ns=SVG_NS,
        uses='<use href="#s" x="10" y="10" width="20" height="20"/>'
             '<use href="#s" x="50" y="40" width="30" height="30"/>')
    ba = template.format(
        ns=SVG_NS,
        uses='<use href="#s" x="50" y="40" width="30" height="30"/>'
             '<use href="#s" x="10" y="10" width="20" height="20"/>')
    assert render(ab) == render(ba)


def test_repeated_renders_are_identical():
    """The same parsed Tree must produce identical bytes on every render."""
    svg = f'''<svg xmlns="{SVG_NS}" width="120" height="100">
      <defs><symbol id="s" viewBox="0 0 10 10">
        <rect width="10" height="10" fill="red"/>
      </symbol></defs>
      <use href="#s" x="10" y="10" width="20" height="20"/>
      <use href="#s" x="50" y="40" width="30" height="30"/>
    </svg>'''
    tree = Tree(bytestring=svg.encode())
    first = surface_render(tree)
    for _ in range(3):
        assert surface_render(tree) == first
    # Two independent parses must also render identically.
    assert surface_render(Tree(bytestring=svg.encode())) == first


def test_original_tree_and_xml_are_not_rewritten():
    """Rendering must not mutate the caller's Tree, the cached trees or the
    ElementTree XML behind them."""
    svg = f'''<svg xmlns="{SVG_NS}" width="120" height="100">
      <defs>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </symbol>
      </defs>
      <use href="#s" x="10" y="10" width="20" height="20"
           viewBox="0 0 5 5" mask="url(#m)"/>
      <use href="#s" x="50" y="40" width="30" height="30"/>
    </svg>'''
    tree = Tree(bytestring=svg.encode())
    use_nodes = [child for child in tree.children if child.tag == 'use']
    assert len(use_nodes) == 2

    def snapshot():
        return [
            (dict(use_node), use_node.tag) for use_node in use_nodes]

    before = snapshot()
    etree_before = _etree_snapshot(tree.xml_tree)
    surface_render(tree)
    surface_render(tree)  # a second pass would crash on stale mutation
    assert snapshot() == before
    assert _etree_snapshot(tree.xml_tree) == etree_before

    # The <use> attributes the old implementation deleted are still present.
    first_use = use_nodes[0]
    assert first_use['x'] == '10'
    assert first_use['y'] == '10'
    assert first_use['viewBox'] == '0 0 5 5'
    assert first_use['mask'] == 'url(#m)'

    # The symbol itself keeps its tag at both the Node and XML level.
    symbol_xml = tree.xml_tree.find(f'.//{{{SVG_NS}}}symbol')
    assert symbol_xml.tag == f'{{{SVG_NS}}}symbol'
    assert symbol_xml.attrib['viewBox'] == '0 0 10 10'


def _etree_snapshot(element):
    """Recursive (tag, attrib, text) snapshot of an ElementTree element."""
    return (
        element.tag,
        dict(element.attrib),
        element.text,
        [_etree_snapshot(child) for child in element],
    )


def test_cached_targets_are_not_polluted():
    """Overrides applied while expanding one reference must never be visible
    to another reference of the same target."""
    svg = f'''<svg xmlns="{SVG_NS}" width="160" height="60">
      <defs><symbol id="s" viewBox="0 0 10 10">
        <rect width="10" height="10" fill="red"/>
      </symbol></defs>
      <rect width="160" height="60" fill="white"/>
      <use href="#s" x="0" y="0" width="20" height="20"/>
      <use href="#s" x="40" y="20" width="40" height="40"/>
    </svg>'''
    image_pixels = pixels(svg)
    # The first instance keeps its own 20x20 size even though the second one
    # overrides width/height to 40 on its own private clone.
    assert_pixels(image_pixels, {
        (10, 10): RED,
        (25, 10): WHITE,
        (60, 40): RED,
        (35, 30): WHITE,
    })


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------

def test_missing_reference_does_not_affect_siblings():
    """A reference to an unknown id is skipped and the sibling rect still
    paints, proving the context transform state was restored."""
    svg = f'''<svg xmlns="{SVG_NS}" width="50" height="50">
      <use href="#does-not-exist" x="999" y="999"/>
      <rect x="10" y="10" width="30" height="30" fill="red"/>
    </svg>'''
    assert_pixels(pixels(svg), {
        (20, 20): RED,
        (1, 1): BLACK,
    })


def test_filtered_reference_does_not_affect_siblings():
    """A reference rejected by conditional processing is cleanly skipped."""
    svg = f'''<svg xmlns="{SVG_NS}" width="50" height="50"
        xmlns:foo="http://example.com/unsupported">
      <use href="#anything"
           requiredExtensions="http://example.com/unsupported"/>
      <rect x="10" y="10" width="30" height="30" fill="green"/>
    </svg>'''
    assert_pixels(pixels(svg), {
        (20, 20): GREEN,
        (1, 1): BLACK,
    })


def test_sibling_after_failed_use_keeps_own_geometry():
    """The (x, y) translate saved for a failed reference cannot leak to the
    next sibling element."""
    svg = f'''<svg xmlns="{SVG_NS}" width="60" height="60">
      <use href="#missing" x="30" y="30"/>
      <rect x="0" y="0" width="10" height="10" fill="blue"/>
    </svg>'''
    assert_pixels(pixels(svg), {
        (5, 5): BLUE,      # not shifted to (35, 35)
        (35, 35): BLACK,
    })


# ---------------------------------------------------------------------------
# External URL policy
# ---------------------------------------------------------------------------

def test_external_reference_blocked_in_safe_mode():
    """Without ``unsafe`` external references go through ``safe_fetch`` and
    resolve to an empty SVG; the document still renders."""
    svg = (f'<svg xmlns="{SVG_NS}" width="6" height="6">'
           '<rect width="6" height="6" fill="white"/>'
           '<use href="file:///nonexistent/path/secret.svg#id"/></svg>')
    assert_pixels(pixels(svg), {
        (3, 3): WHITE,
    })


def test_data_url_reference_allowed_in_safe_mode():
    """data: URLs remain fetchable through ``safe_fetch`` in safe mode."""
    payload = base64.b64encode(
        f'<svg xmlns="{SVG_NS}" width="4" height="4">'
        '<g id="r"><rect width="4" height="4" fill="orange"/></g></svg>'
        .encode()).decode()
    svg = (f'<svg xmlns="{SVG_NS}" width="4" height="4">'
           f'<use href="data:image/svg+xml;base64,{payload}#r"/></svg>')
    assert_pixels(pixels(svg), {(1, 1): ORANGE})


def test_unsafe_uses_custom_url_fetcher():
    """With ``unsafe`` a provided url_fetcher loads the external target and
    the referenced content is drawn."""
    def fetcher(url, resource_type):
        assert resource_type == 'image/svg+xml'
        return (
            f'<svg xmlns="{SVG_NS}" width="4" height="4">'
            '<g id="r"><rect width="4" height="4" fill="purple"/></g></svg>'
        ).encode()

    svg = (f'<svg xmlns="{SVG_NS}" width="4" height="4">'
           '<use href="external.svg#r"/></svg>')
    tree = Tree(bytestring=svg.encode(), unsafe=True, url_fetcher=fetcher)
    output = io.BytesIO()
    PNGSurface(tree, output, 96).finish()
    from PIL import Image
    image_pixels = Image.open(io.BytesIO(output.getvalue())).convert(
        'RGB').load()
    assert image_pixels[1, 1] == PURPLE


def test_safe_mode_does_not_call_default_external_fetcher():
    """A plain Tree in safe mode must replace a default-style fetch with
    safe_fetch (no file/network access) for the external reference."""
    called = []

    def fetcher(url, resource_type):
        called.append(url)
        return b'<svg/>'

    svg = (f'<svg xmlns="{SVG_NS}" width="4" height="4">'
           '<rect width="4" height="4" fill="white"/>'
           '<use href="file:///etc/hostname#x"/></svg>')
    # No explicit url_fetcher: Tree installs safe_fetch internally.
    tree = Tree(bytestring=svg.encode(), unsafe=False)
    assert tree.url_fetcher.__name__ == 'safe_fetch'
    output = io.BytesIO()
    PNGSurface(tree, output, 96).finish()
    assert called == []


# ---------------------------------------------------------------------------
# Reference boundaries: cycle, depth, count
# ---------------------------------------------------------------------------

def test_cyclic_reference_raises_value_error():
    """A reference cycle terminates with a ValueError, never a RecursionError,
    and does not poison later renderings."""
    svg = f'''<svg xmlns="{SVG_NS}" width="10" height="10">
      <g id="a"><use href="#b"/></g>
      <g id="b"><use href="#a"/></g>
      <use href="#a"/>
    </svg>'''
    with pytest.raises(ValueError, match='cyclic'):
        render(svg)

    # A brand new rendering is not poisoned by the previous cycle.
    fresh = (f'<svg xmlns="{SVG_NS}" width="10" height="10">'
             '<rect width="10" height="10" fill="white"/></svg>')
    assert render(fresh)[:8] == b'\x89PNG\r\n\x1a\n'


def test_self_reference_raises_value_error():
    """A <use> pointing at its own ancestor element is a cycle too."""
    svg = f'''<svg xmlns="{SVG_NS}" width="10" height="10">
      <g id="a"><use href="#a"/></g>
      <use href="#a"/>
    </svg>'''
    with pytest.raises(ValueError, match='cyclic'):
        render(svg)


def test_empty_href_is_handled_as_safety_error():
    """An empty href resolves to the document root and thus re-enters the
    current expansion path: it must raise the cycle safety error instead of
    exhausting the Python recursion stack."""
    svg = (f'<svg xmlns="{SVG_NS}" width="10" height="10">'
           '<rect width="10" height="10"/><use href=""/></svg>')
    with pytest.raises(ValueError, match='cyclic'):
        render(svg)


def test_diamond_references_are_not_cycles():
    """The same element reached through two sibling branches is a DAG, not a
    cycle, and must render."""
    svg = f'''<svg xmlns="{SVG_NS}" width="40" height="20">
      <defs>
        <g id="leaf"><rect width="10" height="10" fill="red"/></g>
        <g id="join">
          <use href="#leaf" x="0" y="0"/>
          <use href="#leaf" x="10" y="0"/>
        </g>
      </defs>
      <rect width="40" height="20" fill="white"/>
      <use href="#join"/>
      <use href="#join" x="20" y="0"/>
    </svg>'''
    assert_pixels(pixels(svg), {
        (5, 5): RED,
        (15, 5): RED,
        (25, 5): RED,
        (35, 5): RED,
    })


def _chain_svg(depth):
    """Linear chain of ``depth`` nested <use> expansions plus a leaf."""
    parts = [f'<svg xmlns="{SVG_NS}" width="10" height="10">']
    for index in range(depth):
        parts.append(f'<g id="g{index}"><use href="#g{index + 1}"/></g>')
    parts.append(f'<g id="g{depth}"><rect width="1" height="1"/></g>')
    parts.append('<use href="#g0"/></svg>')
    return ''.join(parts)


def test_reference_depth_limit():
    """Safe mode caps simultaneously active references at 128 levels."""
    # depth 127 -> 128 active <use> expansions: allowed
    render(_chain_svg(127))
    # depth 128 -> 129 active expansions: rejected before Python recursion
    # limits are reached.
    with pytest.raises(ValueError, match='nesting is deeper'):
        render(_chain_svg(128))


def test_unsafe_bypasses_depth_limit():
    """unsafe documents keep the historical behavior: no CairoSVG depth
    cap (Python's own recursion limit remains the only boundary)."""
    render(_chain_svg(200), unsafe=True)


def test_reference_count_limit():
    """Safe mode aborts after 100 000 actually expanded references."""
    # L_k holds two references to L_{k-1}; L_0 is a leaf. The number of
    # <use> expansions reached through L_k is E(k) = 2^(k+1) - 2.
    def expansions(level):
        return 2 ** (level + 1) - 2

    assert expansions(15) == 65534
    assert expansions(16) == 131070

    def document(target):
        parts = [f'<svg xmlns="{SVG_NS}" width="10" height="10"><defs>']
        parts.append('<g id="L0"><rect width="1" height="1"/></g>')
        for level in range(1, 17):
            parts.append(
                f'<g id="L{level}">'
                f'<use href="#L{level - 1}"/><use href="#L{level - 1}"/></g>')
        parts.append('</defs>')
        # A root <use> of L_k costs E(k) + 1 expansions; decompose target.
        remaining = target
        for level in range(16, -1, -1):
            block = expansions(level) + 1
            while remaining >= block:
                parts.append(f'<use href="#L{level}"/>')
                remaining -= block
        assert remaining == 0
        parts.append('</svg>')
        return ''.join(parts)

    render(document(100_000))
    with pytest.raises(ValueError, match='100.000 referenced elements'):
        render(document(100_001))


def test_unsafe_bypasses_reference_count_limit():
    """unsafe documents may expand more than 100 000 references."""
    svg = f'''<svg xmlns="{SVG_NS}" width="10" height="10"><defs>
      <g id="L0"><rect width="1" height="1"/></g>
      <g id="L1"><use href="#L0"/><use href="#L0"/></g>
    </defs>
    <use href="#L1"/></svg>'''
    # Small document rendered with a tiny monkeypatched limit proves the
    # unsafe flag bypasses the count check without building a huge tree.
    original_limit = defs.MAX_REFERENCES
    defs.MAX_REFERENCES = 1
    try:
        with pytest.raises(ValueError, match='referenced elements'):
            render(svg)
        render(svg, unsafe=True)
    finally:
        defs.MAX_REFERENCES = original_limit


def test_limits_reset_for_each_rendering():
    """Counters and cycle paths start fresh for every new surface."""
    svg = f'''<svg xmlns="{SVG_NS}" width="20" height="20">
      <defs><g id="g"><rect width="10" height="10" fill="red"/></g></defs>
      <use href="#g" x="0" y="0"/>
      <use href="#g" x="10" y="10"/>
    </svg>'''
    expected = render(svg)
    # Many successive renderings must never accumulate counts.
    for _ in range(5):
        assert render(svg) == expected

    # A failed (cyclic) rendering cannot poison the next one.
    cycle = f'''<svg xmlns="{SVG_NS}" width="10" height="10">
      <g id="a"><use href="#a"/></g><use href="#a"/>
    </svg>'''
    with pytest.raises(ValueError):
        render(cycle)
    assert render(svg) == expected
