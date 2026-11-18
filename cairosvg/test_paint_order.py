"""
CairoSVG ``paint-order`` rendering tests.

These tests render real PNG images and compare pixels at overlapping areas.
The fixtures below are reused by the demo script, so the pictures shown in
the documentation are the ones actually tested here.

"""

import io
from pathlib import Path

import pytest
from PIL import Image

from . import svg2png
from .helpers import PAINT_ORDER, paint_order, parse_paint_order
from .parser import Tree

BLUE = (0, 0, 255, 255)
RED = (255, 0, 0, 255)
GREEN = (0, 170, 0, 255)
TRANSPARENT = (0, 0, 0, 0)
DEMO = Path(__file__).parent.parent / 'demo' / 'paint-order.svg'

MARKER = (
    '<marker id="m" markerUnits="userSpaceOnUse" markerWidth="32" '
    'markerHeight="32" refX="16" refY="16" viewBox="0 0 32 32" orient="0">'
    '<rect width="32" height="32" fill="#00aa00"/>'
    '</marker>')


def _panel(x, paint_order_attribute):
    """Return the icon path used by the demo, shifted by ``x``."""
    return (
        f'<path d="M{x + 20} 20 L{x + 100} 20 L{x + 100} 100 '
        f'L{x + 20} 100 L{x + 20} 20" fill="#0000ff" stroke="#ff0000" '
        f'stroke-width="16" marker-end="url(#m)" '
        f'{paint_order_attribute}/>')


def _document(body, width=120, height=120, extra=''):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}"><defs>{MARKER}{extra}</defs>{body}</svg>'
    ).encode()


def render(svg, **kwargs):
    """Render ``svg`` as a PNG and return a Pillow image."""
    return Image.open(io.BytesIO(svg2png(bytestring=svg, **kwargs))).convert(
        'RGBA')


def pixel(image, x, y):
    return image.getpixel((int(x), int(y)))


# Points where the three painting operations overlap, chosen so that the
# visible colour uniquely identifies which operation was painted last.
OVERLAP_POINTS = {
    # Corner where the fill, the stroke and the marker all cover each other.
    'marker-tip': (16, 20),
    # Inside the path, covered by the fill and by the marker.
    'inside': (32, 32),
    # Stroke and fill overlap, no marker there.
    'stroke-fill': (24, 60),
    # Only the fill is painted, same colour for every order.
    'fill-only': (60, 60),
}


def test_paint_order_parser():
    assert parse_paint_order('normal') == PAINT_ORDER
    assert parse_paint_order('stroke') == ('stroke', 'fill', 'markers')
    assert parse_paint_order('markers stroke') == (
        'markers', 'stroke', 'fill')
    assert parse_paint_order('stroke fill markers') == (
        'stroke', 'fill', 'markers')
    assert parse_paint_order('  FILL   Stroke  ') == (
        'fill', 'stroke', 'markers')
    # Invalid values are rejected.
    assert parse_paint_order('') is None
    assert parse_paint_order('stroke bogus') is None
    assert parse_paint_order('fill fill') is None
    assert parse_paint_order(None) is None


def test_paint_order_inheritance():
    """``paint-order`` is inherited, overridden and falls back when invalid."""
    svg = _document(
        '<g paint-order="stroke">'
        '<rect x="10" y="10" width="40" height="40" fill="#0000ff" '
        'stroke="#ff0000" stroke-width="16"/>'
        '<rect x="60" y="10" width="40" height="40" fill="#0000ff" '
        'stroke="#ff0000" stroke-width="16" paint-order="fill"/>'
        '</g>')
    image = render(svg)
    # Inherited from the group: the fill is painted after the stroke.
    assert pixel(image, 14, 30) == BLUE
    # Locally overridden: the stroke is painted last again.
    assert pixel(image, 64, 30) == RED


def test_paint_order_invalid_value_does_not_hide_element():
    """Invalid values are ignored instead of making the node disappear."""
    svg = _document(
        '<rect x="10" y="10" width="40" height="40" fill="#0000ff" '
        'stroke="#ff0000" stroke-width="16" paint-order="stroke bogus"/>')
    image = render(svg)
    # The default order is used, so both paints are visible.
    assert pixel(image, 14, 30) == RED
    assert pixel(image, 30, 30) == BLUE


def test_paint_order_invalid_child_inherits_valid_parent():
    svg = _document(
        '<g paint-order="stroke">'
        '<rect x="10" y="10" width="40" height="40" fill="#0000ff" '
        'stroke="#ff0000" stroke-width="16" paint-order="not-a-keyword"/>'
        '</g>')
    image = render(svg)
    assert pixel(image, 14, 30) == BLUE
    assert pixel(image, 30, 30) == BLUE


def test_paint_order_default_and_normal_match():
    """Unset and ``normal`` produce the same picture."""
    unset = _document(_panel(0, ''))
    normal = _document(_panel(0, 'paint-order="normal"'))
    assert svg2png(bytestring=unset) == svg2png(bytestring=normal)


def test_paint_order_three_orders_differ():
    """Default, stroke-first and markers-first pictures are all different."""
    default = render(_document(_panel(0, '')))
    stroke_first = render(_document(_panel(0, 'paint-order="stroke"')))
    markers_first = render(_document(_panel(0, 'paint-order="markers"')))

    # Default order paints markers after fill and stroke.
    assert pixel(default, *OVERLAP_POINTS['marker-tip']) == GREEN
    assert pixel(default, *OVERLAP_POINTS['stroke-fill']) == RED
    assert pixel(default, *OVERLAP_POINTS['inside']) == GREEN

    # Stroke first, then fill, markers last.
    assert pixel(stroke_first, *OVERLAP_POINTS['stroke-fill']) == BLUE
    assert pixel(stroke_first, *OVERLAP_POINTS['marker-tip']) == GREEN
    assert pixel(stroke_first, *OVERLAP_POINTS['inside']) == GREEN

    # Markers first: fill and stroke cover the marker.
    assert pixel(markers_first, *OVERLAP_POINTS['marker-tip']) == RED
    assert pixel(markers_first, *OVERLAP_POINTS['inside']) == BLUE
    assert pixel(markers_first, *OVERLAP_POINTS['stroke-fill']) == RED

    # Only the fill is visible on the right edge for every order.
    for image in (default, stroke_first, markers_first):
        assert pixel(image, *OVERLAP_POINTS['fill-only']) == BLUE

    # And the three pictures are actually different images.
    assert default.tobytes() != stroke_first.tobytes()
    assert default.tobytes() != markers_first.tobytes()
    assert stroke_first.tobytes() != markers_first.tobytes()


def test_paint_order_keeps_all_subpaths():
    """A path with several subpaths keeps all of them when painted twice."""
    svg = _document(
        '<path d="M5 10 h30 v30 h-30 z M65 10 h30 v30 h-30 z" '
        'fill="#0000ff" stroke="#ff0000" stroke-width="8" '
        'paint-order="stroke fill"/>', width=110, height=60)
    image = render(svg)
    for x in (20, 80):
        # The stroke is painted first and the fill over it.
        assert pixel(image, x, 25) == BLUE
        assert pixel(image, x - 18, 25) == RED
    # The transparent gap between both subpaths stays empty.
    assert pixel(image, 55, 25) == TRANSPARENT


def test_paint_order_with_gradient_clip_and_opacity():
    """Gradients, clip paths and groups follow the same paint order."""
    extra = (
        '<linearGradient id="g">'
        '<stop offset="0" stop-color="#0000ff"/>'
        '<stop offset="1" stop-color="#0000ff"/>'
        '</linearGradient>'
        '<clipPath id="c"><rect x="5" y="5" width="100" height="100"/>'
        '</clipPath>')
    svg = _document(
        '<g clip-path="url(#c)" opacity="0.5">'
        '<rect x="10" y="10" width="60" height="60" fill="url(#g)" '
        'stroke="#ff0000" stroke-width="16" paint-order="stroke"/>'
        '</g>', extra=extra)
    image = render(svg, background_color='white')
    # Half-transparent blue over white, painted after the stroke.
    assert pixel(image, 14, 40) == (127, 127, 255, 255)
    # Same blue in the middle of the shape.
    assert pixel(image, 40, 40) == (127, 127, 255, 255)
    # Outside the clip path, nothing is painted.
    assert pixel(image, 2, 40) == (255, 255, 255, 255)


def test_paint_order_does_not_leak_between_nodes():
    """A node’s path is not reused by its following siblings."""
    svg = _document(
        '<rect x="10" y="10" width="40" height="30" fill="#0000ff" '
        'stroke="#ff0000" stroke-width="14" paint-order="stroke"/>'
        '<path d="m10 60 l30 0 l0 10 l-30 0 z" fill="#00aa00"/>',
        width=120, height=80)
    image = render(svg)
    # Both siblings are drawn at their own position.
    assert pixel(image, 20, 65) == GREEN
    assert pixel(image, 20, 20) == BLUE
    # Nothing is painted where only a leaked path would be.
    assert pixel(image, 50, 65) == TRANSPARENT


@pytest.mark.parametrize('order, expected', (
    (PAINT_ORDER, RED),
    (('stroke', 'fill', 'markers'), BLUE),
))
def test_paint_order_fill_stroke_only(order, expected):
    """Check plain shapes without markers for both simple orders."""
    attribute = 'paint-order="{}"'.format(' '.join(order))
    svg = _document(
        f'<rect x="10" y="10" width="40" height="40" fill="#0000ff" '
        f'stroke="#ff0000" stroke-width="16" {attribute}/>')
    assert pixel(render(svg), 14, 30) == expected


def test_paint_order_node_helper():
    tree = Tree(bytestring=_document(
        '<g paint-order="stroke">'
        '<rect x="0" y="0" width="10" height="10" fill="black"/>'
        '</g>'
        '<rect x="20" y="0" width="10" height="10" fill="black"/>'))
    group = next(child for child in tree.children if child.tag == 'g')
    rect = group.children[0]
    plain = next(
        child for child in tree.children
        if child.tag == 'rect' and child.get('x') == '20')
    assert paint_order(group) == ('stroke', 'fill', 'markers')
    # The value is inherited by children.
    assert paint_order(rect) == ('stroke', 'fill', 'markers')
    # Nodes without a ``paint-order`` keep the default order.
    assert paint_order(plain) == PAINT_ORDER
    assert paint_order(tree) == PAINT_ORDER


def test_paint_order_from_css_and_style_attribute():
    """``paint-order`` works in stylesheets and in ``style`` declarations."""
    stylesheet = (
        '<style>.icon { paint-order: stroke; }</style>'
        '<g class="icon"><rect x="10" y="10" width="40" height="40" '
        'fill="#0000ff" stroke="#ff0000" stroke-width="16"/></g>')
    style_attribute = (
        '<rect x="10" y="10" width="40" height="40" fill="#0000ff" '
        'stroke="#ff0000" stroke-width="16" style="paint-order:stroke"/>')
    for svg in (_document(stylesheet), _document(style_attribute)):
        assert pixel(render(svg), 14, 30) == BLUE


def test_paint_order_on_text():
    """Text is stroked before being filled when requested."""
    template = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="220" height="90">'
        '<text x="10" y="65" font-family="sans-serif" font-size="60" '
        'fill="#0000ff" stroke="#ff0000" stroke-width="4" {}>T</text></svg>')

    def counts(attribute):
        image = render(template.format(attribute).encode())
        colors = image.getcolors(maxcolors=1 << 24)
        result = dict((color, count) for count, color in colors)
        return result.get(RED, 0), result.get(BLUE, 0)

    red_default, blue_default = counts('')
    red_stroke, blue_stroke = counts('paint-order="stroke"')
    # The same glyph is drawn, but the fill now covers the stroke.
    assert red_stroke < red_default
    assert blue_stroke > blue_default


def test_paint_order_demo_uses_same_geometry():
    """The demo only changes paint order and translations, not the shapes."""
    tree = Tree(url=DEMO)
    panels = [
        child for child in tree.children
        if child.tag == 'g' and child.get('id') in (
            'default', 'stroke-first', 'markers-first')]
    assert len(panels) == 3
    paths = [panel.children[0] for panel in panels]
    # Same path, same paint attributes: only the order changes.
    for attribute in ('d', 'fill', 'stroke', 'stroke-width', 'marker-end'):
        assert len({path.get(attribute) for path in paths}) == 1
    orders = [path.get('paint-order') for path in paths]
    assert orders == [None, 'stroke', 'markers']
    # The demo renders and each panel has a distinct overlapping area.
    image = render(DEMO.read_bytes(), scale=2)
    # Marker / stroke / fill overlap at the top-left corner.
    marker_points = ((16, 20), (156, 20), (296, 20))
    marker_values = [pixel(image, x * 2, y * 2) for x, y in marker_points]
    assert marker_values[0] == GREEN    # Default: marker painted last.
    assert marker_values[1] == GREEN    # Stroke first, marker still last.
    assert marker_values[2] == RED      # Markers first: stroke over marker.
    # Stroke / fill overlap on the left edge.
    edge_points = ((24, 60), (164, 60), (304, 60))
    edge_values = [pixel(image, x * 2, y * 2) for x, y in edge_points]
    assert edge_values[0] == RED        # Default: stroke painted last.
    assert edge_values[1] == BLUE       # Stroke first, fill painted over it.
    assert edge_values[2] == RED        # Markers first, stroke still last.
