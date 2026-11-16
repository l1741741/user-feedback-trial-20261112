"""
Tests for gradients inheriting attributes and stops through ``href``.

The gradients are sampled on the rendered PNG, so that the tests check the
real color distribution and not only the internal definitions.

"""

import io
from pathlib import Path

import cairocffi as cairo

from . import parser, surface, svg2png

EXAMPLES = Path(__file__).parent.parent / 'examples' / 'gradient-inheritance'

# A red > blue linear gradient, the shared definition used by most tests.
SHARED = '''
  <linearGradient id="shared" x1="0%" y1="0%" x2="100%" y2="0%">
    <stop offset="0" stop-color="#ff0000"/>
    <stop offset="1" stop-color="#0000ff"/>
  </linearGradient>
'''

REFERRERS = SHARED + '''
  <linearGradient id="vertical" href="#shared" x2="0%" y2="100%"/>
  <radialGradient id="radial" href="#shared" cx="50%" cy="50%" r="50%"/>
  <linearGradient id="own" href="#vertical">
    <stop offset="0" stop-color="#00ff00"/>
    <stop offset="1" stop-color="#000000"/>
  </linearGradient>
'''


def render(svg):
    """Render ``svg`` and return a function sampling colors by coordinates."""
    png = svg2png(svg.encode())
    image = cairo.ImageSurface.create_from_png(io.BytesIO(png))
    data = bytes(image.get_data())
    stride = image.get_stride()

    def pixel(x, y):
        """Return the RGBA tuple of the pixel at (x, y)."""
        offset = y * stride + x * 4
        blue, green, red, alpha = data[offset:offset + 4]
        if alpha not in (0, 255):
            # Cairo stores premultiplied colors, undo it for readability.
            red = min(255, round(red * 255 / alpha))
            green = min(255, round(green * 255 / alpha))
            blue = min(255, round(blue * 255 / alpha))
        return red, green, blue, alpha

    return pixel


def wrap(body, defs=SHARED, width=500, height=100):
    """Build an SVG document with ``defs`` and ``body``."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}">'
        f'<defs>{defs}</defs>{body}</svg>')


def test_href_inherits_coordinates_and_stops():
    """A referrer inherits the stops and the coordinates it does not set."""
    svg = wrap('''
      <linearGradient id="vertical" href="#shared" x2="0%" y2="100%"/>
      <rect x="0" y="0" width="100" height="100" fill="url(#vertical)"/>
      <rect x="100" y="0" width="100" height="100" fill="url(#shared)"/>
    ''')
    sample = render(svg)

    # Referrer: red on top, blue at the bottom, constant on a row.
    assert sample(50, 1)[0] > sample(50, 1)[2]
    assert sample(50, 98)[2] > sample(50, 98)[0]
    assert sample(10, 1) == sample(90, 1) == sample(50, 1)
    middle = sample(50, 50)
    assert abs(middle[0] - middle[2]) < 6

    # Shared definition is untouched: red on the left, blue on the right.
    assert sample(101, 50)[0] > sample(101, 50)[2]
    assert sample(198, 50)[2] > sample(198, 50)[0]
    assert sample(101, 1) == sample(101, 98) == sample(101, 50)


def test_href_keeps_own_stops():
    """Own stops win over the inherited ones."""
    svg = wrap('''
      <linearGradient id="own" href="#shared">
        <stop offset="0" stop-color="#00ff00"/>
        <stop offset="1" stop-color="#000000"/>
      </linearGradient>
      <rect x="0" y="0" width="100" height="100" fill="url(#own)"/>
      <rect x="100" y="0" width="100" height="100" fill="url(#shared)"/>
    ''')
    sample = render(svg)

    left, right = sample(5, 50), sample(95, 50)
    assert left[1] > 200 and left[0] < 20 and left[2] < 20
    assert right[0] < 20 and right[2] < 20

    # The shared definition still uses its own stops.
    left, right = sample(105, 50), sample(195, 50)
    assert left[0] > 200 and left[1] < 20 and left[2] < 20
    assert right[2] > 200 and right[0] < 20


def test_href_multiple_levels():
    """Inheritance follows the whole chain, nearest definition winning."""
    svg = wrap('''
      <linearGradient id="middle" href="#shared" x2="0%" y2="100%"/>
      <linearGradient id="leaf" href="#middle" y2="50%"/>
      <rect x="0" y="0" width="100" height="100" fill="url(#leaf)"/>
    ''')
    sample = render(svg)

    assert sample(50, 1)[0] > sample(50, 1)[2]
    # Past y2="50%", the default pad spread method keeps the last stop color.
    assert sample(50, 50) == sample(50, 98)
    assert sample(50, 50)[2] > sample(50, 50)[0]


def test_href_uses_nearest_stops():
    """Stops come from the nearest definition providing them."""
    defs = '''
      <linearGradient id="root">
        <stop offset="0" stop-color="#ff0000"/>
        <stop offset="1" stop-color="#0000ff"/>
      </linearGradient>
      <linearGradient id="middle" href="#root">
        <stop offset="0" stop-color="#00ff00"/>
        <stop offset="1" stop-color="#000000"/>
      </linearGradient>
      <linearGradient id="leaf" href="#middle"/>
    '''
    body = '''
      <rect x="0" width="100" height="100" fill="url(#leaf)"/>
      <rect x="100" width="100" height="100" fill="url(#root)"/>
    '''
    template = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
        '<defs>{}</defs>{}</svg>')

    leaf = render(template.format(defs, body))
    # Leaf inherits the middle stops, not the root ones.
    assert leaf(5, 50)[1] > 200 and leaf(5, 50)[0] < 20
    assert leaf(95, 50)[0] < 20 and leaf(95, 50)[2] < 20

    root = render(template.format(defs, body))
    assert root(105, 50)[0] > 200 and root(105, 50)[1] < 20
    assert root(195, 50)[2] > 200 and root(195, 50)[0] < 20


def test_href_drawing_order_does_not_change_result():
    """Shared definitions keep the same rendering whatever the draw order."""
    def build(order):
        icons = {
            'shared': (
                '<rect x="0" y="0" width="90" height="100" '
                'fill="url(#shared)"/>'),
            'vertical': (
                '<rect x="100" y="0" width="90" height="100" '
                'fill="url(#vertical)"/>'),
            'radial': (
                '<rect x="200" y="0" width="90" height="100" opacity="0.5" '
                'fill="url(#radial)"/>'),
            'transformed': (
                '<g transform="translate(300, 10) scale(1, 0.8)">'
                '<rect width="90" height="100" fill="url(#vertical)"/></g>'),
            'own': (
                '<rect x="410" y="0" width="90" height="100" '
                'fill="url(#own)"/>'),
        }
        return wrap(
            ''.join(icons[name] for name in order), REFERRERS,
            width=500, height=100)

    all_names = ['shared', 'vertical', 'radial', 'transformed', 'own']
    positions = [
        (5, 5), (45, 50), (85, 95), (105, 5), (105, 50), (105, 95),
        (205, 50), (245, 50), (285, 50), (305, 50), (345, 50), (385, 79),
        (415, 5), (455, 50), (495, 95)]
    reference = render(build(all_names))
    expected = [reference(x, y) for x, y in positions]

    for order in (
            list(reversed(all_names)),
            ['own', 'vertical', 'shared', 'transformed', 'radial'],
            ['radial', 'transformed', 'own', 'shared', 'vertical'],
            ['transformed', 'own', 'shared', 'radial', 'vertical']):
        sample = render(build(order))
        assert [sample(x, y) for x, y in positions] == expected


def test_href_cycles_and_missing_references_are_ignored():
    """Cycles and missing href end safely and do not pollute other shapes."""
    defs = '''
      <linearGradient id="loop-a" href="#loop-b" x1="0%" x2="100%">
        <stop offset="0" stop-color="#ff0000"/>
        <stop offset="1" stop-color="#0000ff"/>
      </linearGradient>
      <linearGradient id="loop-b" href="#loop-a" x2="0%" y2="100%"/>
      <linearGradient id="self" href="#self">
        <stop offset="0" stop-color="#00ff00"/>
        <stop offset="1" stop-color="#000000"/>
      </linearGradient>
      <linearGradient id="missing" href="#nowhere" x1="0%" x2="100%">
        <stop offset="0" stop-color="#ffffff"/>
        <stop offset="1" stop-color="#0000ff"/>
      </linearGradient>
    '''
    body = '''
      <rect x="0" y="0" width="90" height="100" fill="url(#loop-a)"/>
      <rect x="100" y="0" width="90" height="100" fill="url(#loop-b)"/>
      <rect x="200" y="0" width="90" height="100" fill="url(#self)"/>
      <rect x="300" y="0" width="90" height="100" fill="url(#missing)"/>
      <rect x="400" y="0" width="90" height="100" fill="url(#undefined)"/>
    '''
    sample = render(wrap(body, defs))

    # loop-a keeps its own stops: horizontal red > blue.
    assert sample(5, 50)[0] > sample(85, 50)[0]
    # loop-b inherits the stops of loop-a and keeps its own vertical
    # coordinates: red on top, blue at the bottom.
    assert sample(145, 1)[0] > sample(145, 98)[0]
    assert sample(145, 98)[2] > sample(145, 1)[2]
    # Self reference keeps its own stops: green > black.
    assert sample(205, 50)[1] > 200
    assert sample(285, 50)[1] < 20
    # Missing href target keeps the stops declared by the referrer.
    assert sample(305, 50)[0] > 200 and sample(385, 50)[2] > 200
    # Undefined paint server: nothing is painted.
    assert sample(405, 50) == (0, 0, 0, 0)


def test_href_units_spread_and_transforms():
    """Coordinates, spread method and transform are inherited and applied."""
    stops = (
        '<stop offset="0" stop-color="#ff0000"/>'
        '<stop offset="1" stop-color="#0000ff"/>')
    defs = f'''
      <linearGradient id="user-shared" x1="0" y1="0" x2="25" y2="0"
                      gradientUnits="userSpaceOnUse" spreadMethod="reflect">
        {stops}
      </linearGradient>
      <linearGradient id="user" href="#user-shared"/>
      <linearGradient id="transformed" href="#user-shared"
                      gradientTransform="rotate(90 12.5 0)"/>
      <radialGradient id="radial" cx="50" cy="50" r="40"
                      gradientUnits="userSpaceOnUse">
        {stops}
      </radialGradient>
    '''
    template = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        f'<defs>{defs}</defs>{{}}</svg>')

    def collect(gradient_id):
        sample = render(template.format(
            f'<rect width="100" height="100" fill="url(#{gradient_id})"/>'))
        return sample

    # userSpaceOnUse coordinates are inherited: 25 pixels wide, reflect.
    sample = collect('user')
    starts, ends, middle = sample(0, 50), sample(25, 50), sample(12, 50)
    assert starts[0] > starts[2]
    assert ends[2] > ends[0]
    assert abs(middle[0] - middle[2]) < 8
    # Reflect mirrors the gradient beyond its end.
    assert sample(37, 50)[2] > sample(37, 50)[0]
    assert sample(72, 50)[2] > sample(72, 50)[0]
    # The inherited gradientTransform rotates the whole gradient: the
    # variation is now vertical instead of horizontal.
    rotated = collect('transformed')
    assert rotated(0, 0) != rotated(0, 99)
    assert rotated(0, 0) == rotated(99, 50)

    # The inherited radial geometry and units are applied.
    radial = collect('radial')
    assert radial(50, 50)[0] > radial(50, 50)[2]
    assert radial(10, 50)[2] > radial(10, 50)[0]


def test_href_flattened_equivalence():
    """A referrer renders like a definition declaring the same values."""
    stops = (
        '<stop offset="0" stop-color="#ff0000"/>'
        '<stop offset="1" stop-color="#0000ff"/>')
    template = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<defs>'
        '<linearGradient id="base" x1="0" y1="0" x2="50" y2="0" '
        f'spreadMethod="reflect" gradientUnits="userSpaceOnUse">{stops}'
        '</linearGradient>'
        '{}</defs></svg>')

    def collect(markup):
        sample = render(template.format(markup))
        return [sample(x, y) for x in (0, 25, 50, 74, 99)
                for y in (0, 50, 99)]

    inherited = (
        '<linearGradient id="href" href="#base" '
        'gradientTransform="rotate(30 0.5 0.5)"/>'
        '<rect width="100" height="100" fill="url(#href)"/>')
    flattened = (
        '<linearGradient id="flat" x1="0" y1="0" x2="50" y2="0" '
        'spreadMethod="reflect" gradientUnits="userSpaceOnUse" '
        'gradientTransform="rotate(30 0.5 0.5)">'
        f'{stops}</linearGradient>'
        '<rect width="100" height="100" fill="url(#flat)"/>')
    assert collect(inherited) == collect(flattened)


def test_href_does_not_modify_stored_definitions():
    """Resolving a href leaves the shared definitions untouched."""
    svg = wrap('''
      <linearGradient id="vertical" href="#shared" x2="0%" y2="100%"/>
      <rect x="0" y="0" width="100" height="100" fill="url(#vertical)"/>
      <rect x="100" y="0" width="100" height="100" fill="url(#shared)"/>
      <rect x="200" y="0" width="100" height="100" fill="url(#vertical)"/>
    ''')
    tree = parser.Tree(bytestring=svg.encode())
    file_like = io.BytesIO()
    png_surface = surface.PNGSurface(tree, file_like, 96)

    def gradient(name):
        """Return the stored definition, as a plain mapping."""
        return dict(png_surface.gradients[name])

    shared = gradient('shared')
    referrer = gradient('vertical')
    png_surface.finish()

    assert shared == {
        'id': 'shared', 'x1': '0%', 'y1': '0%', 'x2': '100%', 'y2': '0%'}
    assert referrer == {
        'id': 'vertical', 'href': '#shared', 'x2': '0%', 'y2': '100%'}
    # Both references to the vertical gradient look the same, and the shared
    # definition still has its own horizontal direction.
    reference = render(svg)
    assert all(
        reference(x, y) == reference(x + 200, y)
        for x in (5, 50, 95) for y in (5, 50, 95))
    assert reference(5, 50)[0] != reference(195, 50)[0]


def test_example_draw_order():
    """The bundled examples are rendered identically after reordering."""
    normal = svg2png(url=EXAMPLES / 'icons.svg')
    reordered = svg2png(url=EXAMPLES / 'icons-reordered.svg')
    assert normal == reordered


def assert_close(sample, x, y, expected):
    """Check that the pixel at (x, y) is ``expected``, up to rounding."""
    assert sample(x, y) == expected, (
        f'pixel ({x}, {y}) is {sample(x, y)}, expected {expected}')


def test_existing_gradients_are_unchanged():
    """Regression: gradients without href still sample the same colors."""
    stops = (
        '<stop offset="0" stop-color="#ff0000"/>'
        '<stop offset="1" stop-color="#0000ff"/>')

    # objectBoundingBox linear gradient: red on the left, blue on the right.
    sample = render(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        f'<defs><linearGradient id="g">{stops}</linearGradient></defs>'
        '<rect width="100" height="100" fill="url(#g)"/></svg>')
    for x, expected in (
            (5, (241, 0, 14, 255)),
            (25, (190, 0, 65, 255)),
            (50, (126, 0, 129, 255)),
            (75, (63, 0, 192, 255)),
            (95, (12, 0, 243, 255))):
        assert_close(sample, x, 50, expected)

    # Radial gradient in objectBoundingBox units.
    sample = render(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<defs><radialGradient id="g">'
        '<stop offset="0" stop-color="#ffff00"/>'
        '<stop offset="1" stop-color="#000000"/>'
        '</radialGradient></defs>'
        '<rect width="100" height="100" fill="url(#g)"/></svg>')
    assert_close(sample, 50, 50, (251, 251, 0, 255))
    assert_close(sample, 25, 50, (130, 130, 0, 255))
    assert_close(sample, 5, 5, (0, 0, 0, 255))

    # userSpaceOnUse coordinates with the reflect spread method.
    sample = render(
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
        '<defs><linearGradient id="g" gradientUnits="userSpaceOnUse" '
        'x1="10%" y1="0" x2="50%" y2="0" spreadMethod="reflect">'
        f'{stops}</linearGradient></defs>'
        '<rect width="200" height="100" fill="url(#g)"/></svg>')
    assert_close(sample, 10, 5, (225, 0, 30, 255))
    assert_close(sample, 50, 50, (158, 0, 97, 255))
    assert_close(sample, 100, 50, (2, 0, 253, 255))
    assert_close(sample, 150, 50, (161, 0, 94, 255))
    assert_close(sample, 190, 50, (222, 0, 33, 255))

    # Stops with their own offsets and stop-opacity.
    sample = render(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<defs><linearGradient id="g">'
        '<stop offset="0" stop-color="#00ff00"/>'
        '<stop offset="0.25" stop-color="#00ff00" stop-opacity="0.5"/>'
        '<stop offset="0.8" stop-color="#000000"/>'
        '<stop offset="1" stop-color="#ffffff"/>'
        '</linearGradient></defs>'
        '<rect width="100" height="100" fill="url(#g)"/></svg>')
    assert_close(sample, 5, 5, (0, 255, 0, 227))
    assert_close(sample, 25, 50, (0, 251, 0, 129))
    assert_close(sample, 50, 50, (0, 136, 0, 187))
    assert_close(sample, 95, 50, (197, 197, 197, 255))
