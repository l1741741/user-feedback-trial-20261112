"""
Tests for gradient (and pattern) inheritance through ``href`` references.

Brand icons commonly share public ``linearGradient`` / ``radialGradient``
definitions and reference them with ``href``/``xlink:href`` while overriding a
few attributes or defining their own color stops. These tests render real
PNG output and sample actual pixels to guarantee that:

- a referencing node inherits the attributes it does not define and keeps its
  own attributes and stops;
- the same public definition can be referenced by different shapes, with
  different opacities and transforms, in any drawing order without changing
  the color distribution of any icon;
- multi-level inheritance resolves to the effective definition;
- cyclic and missing references terminate safely and never pollute other
  shapes;
- object bounding box and user space coordinate systems and spread methods
  behave as specified.

"""

import io

import cairocffi as cairo

from . import svg2png

WIDTH = HEIGHT = 64
ICON = 64

# A public linear gradient (blue -> red) and a public radial one.
COMMON_DEFS = '''
  <linearGradient id="common-linear" x1="0%" y1="0%" x2="100%" y2="0%">
    <stop offset="0" stop-color="#0000ff"/>
    <stop offset="1" stop-color="#ff0000"/>
  </linearGradient>
  <radialGradient id="common-radial" cx="50%" cy="50%" r="50%">
    <stop offset="0" stop-color="#ffffff"/>
    <stop offset="1" stop-color="#000000"/>
  </radialGradient>
'''

# Extra definitions that reference the public ones with their own overrides.
OVERRIDE_DEFS = '''
  <linearGradient id="vertical-own" href="#common-linear"
                  x1="0%" y1="0%" x2="0%" y2="100%">
    <stop offset="0" stop-color="#00ff00"/>
    <stop offset="1" stop-color="#ffff00"/>
  </linearGradient>
  <radialGradient id="radial-scaled" href="#common-radial"
                  gradientTransform="scale(0.5, 1)"/>
'''

# Icons referencing the shared definitions. Each one uses a different shape,
# a different override and a different opacity/transform.
ICON_BODIES = {
    # Plain reference: inherits the public stops and horizontal direction.
    'plain': '''
    <rect x="0" y="0" width="64" height="64" fill="url(#common-linear)"/>
    ''',
    # Overrides coordinates (vertical) but keeps its own color stops.
    'override-stops': '''
    <rect x="0" y="0" width="64" height="64" fill="url(#vertical-own)"/>
    ''',
    # Different shape and opacity, only inheriting the public definition.
    'circle-opacity': '''
    <circle cx="32" cy="32" r="32" fill="url(#common-linear)" opacity="0.5"/>
    ''',
    # Transformed radial reference.
    'radial-transform': '''
    <rect x="0" y="0" width="64" height="64" fill="url(#radial-scaled)"/>
    ''',
}


def rgba_pixel(surface, x, y):
    """Return the ``(r, g, b, a)`` values of a pixel.

    cairocffi exposes the native little-endian ARGB buffer, i.e. the bytes
    are stored in B, G, R, A order.

    """
    data = surface.get_data()
    offset = y * surface.get_stride() + x * 4
    blue, green, red, alpha = data[offset:offset + 4]
    return (red, green, blue, alpha)


def render(body, defs='', width=WIDTH, height=HEIGHT,
           gradient_units=None, spread_method=None):
    """Render an SVG document and return an ``ImageSurface``."""
    extra_defs = ''
    if gradient_units or spread_method:
        units = (f'gradientUnits="{gradient_units}"'
                 if gradient_units else '')
        spread = (f'spreadMethod="{spread_method}"'
                  if spread_method else '')
        extra_defs = f'''
  <linearGradient id="user-gradient" {units} {spread}
      x1="0" y1="0" x2="32" y2="0">
    <stop offset="0" stop-color="#ff0000"/>
    <stop offset="1" stop-color="#0000ff"/>
  </linearGradient>'''
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg"
        xmlns:xlink="http://www.w3.org/1999/xlink"
        width="{width}" height="{height}" viewBox="0 0 {width} {height}">
      <defs>{COMMON_DEFS}{OVERRIDE_DEFS}{extra_defs}{defs}</defs>
      {body}
    </svg>'''
    png = svg2png(bytestring=svg.encode(), output_width=width,
                  output_height=height)
    return cairo.ImageSurface.create_from_png(io.BytesIO(png))


def pixels(surface):
    """Return the raw bytes of a surface."""
    return bytes(surface.get_data())


def icon_samples(surface, index):
    """Extract the ``(r, g, b, a)`` samples of one 64px-wide icon slot."""
    x = index * ICON + ICON // 2
    return [rgba_pixel(surface, x, y) for y in (8, 24, 40, 56)]


def icon_document(order):
    """Build a document with side-by-side icons in the given order."""
    bodies = '\n'.join(
        f'<g transform="translate({i * ICON}, 0)">{ICON_BODIES[name]}</g>'
        for i, name in enumerate(order))
    return render(bodies, width=ICON * len(order))


def test_shared_gradient_is_order_independent():
    """Swapping icon nodes keeps each icon's color distribution identical."""
    order_a = ['plain', 'override-stops', 'circle-opacity',
               'radial-transform']
    order_b = list(reversed(order_a))
    surface_a = icon_document(order_a)
    surface_b = icon_document(order_b)

    for name in order_a:
        # The same icon must sample the same colors whatever its slot and
        # whatever the drawing order.
        samples_a = icon_samples(surface_a, order_a.index(name))
        samples_b = icon_samples(surface_b, order_b.index(name))
        assert samples_a == samples_b, (
            f'icon {name!r} changes with drawing order: '
            f'{samples_a} != {samples_b}')


def test_override_keeps_own_stops_and_coordinates():
    """An override uses its own stops and its own direction."""
    surface = render(ICON_BODIES['override-stops'])
    top = rgba_pixel(surface, 32, 4)
    bottom = rgba_pixel(surface, 32, 60)
    left = rgba_pixel(surface, 4, 32)
    right = rgba_pixel(surface, 60, 32)

    # Vertical green -> yellow: green at the top, yellow at the bottom.
    assert top[1] > 200 and top[0] < 80
    assert bottom[0] > 200 and bottom[1] > 200 and bottom[2] < 80
    # A vertical gradient does not vary along the horizontal axis.
    assert left == right


def test_plain_reference_inherits_public_stops_and_direction():
    """A reference without stops inherits the public blue -> red gradient."""
    surface = render(ICON_BODIES['plain'])
    left = rgba_pixel(surface, 4, 32)
    right = rgba_pixel(surface, 60, 32)

    # Blue on the left, red on the right.
    assert left[2] > 200 and left[0] < 80
    assert right[0] > 200 and right[2] < 80
    # The horizontal gradient does not vary along the vertical axis.
    assert rgba_pixel(surface, 32, 4) == rgba_pixel(surface, 32, 60)


def test_multilevel_inheritance_resolves_effective_definition():
    """A chain of references merges stops and attributes from its ancestors."""
    defs = '''
  <linearGradient id="base" x1="0%" y1="0%" x2="0%" y2="100%">
    <stop offset="0" stop-color="#0000ff"/>
    <stop offset="1" stop-color="#ff0000"/>
  </linearGradient>
  <linearGradient id="middle" href="#base"/>
  <linearGradient id="leaf-own" href="#middle">
    <stop offset="0" stop-color="#00ff00"/>
    <stop offset="1" stop-color="#ffff00"/>
  </linearGradient>'''
    body = '''
    <rect x="0" y="0" width="32" height="64" fill="url(#leaf-own)"/>
    <rect x="32" y="0" width="32" height="64" fill="url(#middle)"/>'''

    def document():
        svg = f'''<svg xmlns="http://www.w3.org/2000/svg"
            width="64" height="64" viewBox="0 0 64 64">
          <defs>{defs}</defs>{body}
        </svg>'''
        png = svg2png(bytestring=svg.encode(), output_width=64,
                      output_height=64)
        return cairo.ImageSurface.create_from_png(io.BytesIO(png))

    # The chain must resolve deterministically across documents.
    assert pixels(document()) == pixels(document())
    surface = document()

    # The leaf keeps its own green -> yellow stops and the inherited vertical
    # direction of the base definition.
    leaf_top = rgba_pixel(surface, 16, 4)
    leaf_bottom = rgba_pixel(surface, 16, 60)
    assert leaf_top[1] > 200 and leaf_top[0] < 80
    assert leaf_bottom[0] > 200 and leaf_bottom[1] > 200
    # The middle inherits the base blue -> red vertical gradient.
    middle_top = rgba_pixel(surface, 48, 4)
    middle_bottom = rgba_pixel(surface, 48, 60)
    assert middle_top[2] > 200 and middle_top[0] < 80
    assert middle_bottom[0] > 200 and middle_bottom[2] < 80


def test_chain_resolution_is_order_independent():
    """Resolving a referenced node first must not alter later references."""
    defs = '''
  <linearGradient id="common" x1="0%" y1="0%" x2="100%" y2="0%">
    <stop offset="0" stop-color="#0000ff"/>
    <stop offset="1" stop-color="#ff0000"/>
  </linearGradient>
  <linearGradient id="gb" href="#common" x1="0%" y1="0%" x2="0%" y2="100%"/>
  <linearGradient id="ga" href="#gb">
    <stop offset="0" stop-color="#00ff00"/>
    <stop offset="1" stop-color="#ffff00"/>
  </linearGradient>'''
    template = '''<svg xmlns="http://www.w3.org/2000/svg"
        width="192" height="64" viewBox="0 0 192 64">
      <defs>{defs}</defs>
      {body}
    </svg>'''
    order_leaf_first = template.format(
        defs=defs, body='''
      <rect x="0" width="64" height="64" fill="url(#ga)"/>
      <rect x="64" width="64" height="64" fill="url(#gb)"/>
      <rect x="128" width="64" height="64" fill="url(#common)"/>''')
    order_middle_first = template.format(
        defs=defs, body='''
      <rect x="64" width="64" height="64" fill="url(#gb)"/>
      <rect x="0" width="64" height="64" fill="url(#ga)"/>
      <rect x="128" width="64" height="64" fill="url(#common)"/>''')

    def png(document):
        return cairo.ImageSurface.create_from_png(io.BytesIO(svg2png(
            bytestring=document.encode(), output_width=192,
            output_height=64)))

    assert pixels(png(order_leaf_first)) == pixels(png(order_middle_first))


def test_file_url_does_not_poison_shared_tree_cache(tmp_path):
    """Rendering from a file URL must not corrupt the shared tree cache.

    When the document has a real URL, merged href trees used to register
    themselves in the surface ``tree_cache`` under the document URL and the
    referenced id. A later reference to that id then resolved to the merged
    node of an earlier icon, producing an empty/black shape. Rendering from a
    file must give the same result as rendering the same bytes directly.
    """
    svg = '''<svg xmlns="http://www.w3.org/2000/svg"
        width="192" height="64" viewBox="0 0 192 64">
      <defs>
        <linearGradient id="base" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0" stop-color="#0000ff"/>
          <stop offset="1" stop-color="#ff0000"/>
        </linearGradient>
        <linearGradient id="mid" href="#base"
                        x1="0%" y1="0%" x2="0%" y2="100%"/>
        <linearGradient id="leaf" href="#mid">
          <stop offset="0" stop-color="#00ff00"/>
          <stop offset="1" stop-color="#ffff00"/>
        </linearGradient>
      </defs>
      <rect x="0" width="64" height="64" fill="url(#leaf)"/>
      <rect x="64" width="64" height="64" fill="url(#mid)"/>
      <rect x="128" width="64" height="64" fill="url(#base)"/>
    </svg>'''
    svg_path = tmp_path / 'cache-chain.svg'
    svg_path.write_text(svg)

    from_bytes = svg2png(bytestring=svg.encode(),
                         output_width=192, output_height=64)
    from_file = svg2png(url=str(svg_path),
                        output_width=192, output_height=64)
    assert from_file == from_bytes

    surface = cairo.ImageSurface.create_from_png(io.BytesIO(from_file))
    # The directly-painted mid gradient keeps its opaque blue -> red stops.
    mid_top = rgba_pixel(surface, 96, 8)
    mid_bottom = rgba_pixel(surface, 96, 56)
    assert mid_top[2] > 150 and mid_top[0] < 120 and mid_top[3] == 255
    assert mid_bottom[0] > 150 and mid_bottom[2] < 120 and \
        mid_bottom[3] == 255


def test_opacity_override_does_not_pollute_shared_definition():
    """One icon's opacity must not change another icon using the same def."""
    body = '''
    <rect x="0" y="0" width="64" height="64" fill="url(#common-linear)"
          opacity="0.25"/>
    <rect x="64" y="0" width="64" height="64" fill="url(#common-linear)"/>'''
    surface = render(body, width=128)

    translucent = rgba_pixel(surface, 32, 32)
    opaque = rgba_pixel(surface, 96, 32)
    assert translucent[3] < opaque[3]
    assert opaque[3] == 255


def test_user_space_on_use_coordinates():
    """userSpaceOnUse coordinates are interpreted in the user space."""
    body = '''
    <rect x="10" y="10" width="44" height="44" fill="url(#user-gradient)"/>'''
    object_bbox = render(body, gradient_units='objectBoundingBox')
    user_space = render(body, gradient_units='userSpaceOnUse')
    # The coordinate systems differ, so the sampled colors must differ.
    assert pixels(object_bbox) != pixels(user_space)


def test_spread_method_repeat_differs_from_pad():
    """The spread method is honored outside the gradient vector."""
    body = '''
    <rect x="0" y="0" width="64" height="64" fill="url(#user-gradient)"/>'''
    pad = render(body, gradient_units='userSpaceOnUse',
                 spread_method='pad')
    repeat = render(body, gradient_units='userSpaceOnUse',
                    spread_method='repeat')
    # The gradient vector only covers the first half of the rect: repeat must
    # restart the gradient while pad keeps the last stop color.
    assert pixels(pad) != pixels(repeat)


def test_cyclic_reference_terminates_safely():
    """A reference loop renders without raising and keeps other shapes."""
    defs = '''
  <linearGradient id="cycle-1" href="#cycle-2">
    <stop offset="0" stop-color="#00ff00"/>
    <stop offset="1" stop-color="#008800"/>
  </linearGradient>
  <linearGradient id="cycle-2" href="#cycle-1"/>'''
    body = '''
    <rect x="0" y="0" width="64" height="64" fill="url(#cycle-1)"/>
    <rect x="64" y="0" width="64" height="64" fill="url(#cycle-2)"/>
    <rect x="128" y="0" width="64" height="64" fill="red"/>'''
    surface = render(body, defs=defs, width=192)

    # Both cyclic nodes resolve to the same effective definition and the
    # unrelated red rect is painted untouched.
    assert rgba_pixel(surface, 32, 32) == rgba_pixel(surface, 96, 32)
    assert rgba_pixel(surface, 160, 32) == (255, 0, 0, 255)


def test_missing_reference_terminates_safely():
    """A missing href uses the node's own content and never raises."""
    defs = '''
  <linearGradient id="broken" href="#does-not-exist">
    <stop offset="0" stop-color="#ff00ff"/>
    <stop offset="1" stop-color="#880088"/>
  </linearGradient>'''
    body = '''
    <rect x="0" y="0" width="64" height="64" fill="url(#broken)"/>
    <rect x="64" y="0" width="64" height="64" fill="red"/>'''
    surface = render(body, defs=defs, width=128)

    # The broken gradient still paints (a single flat color from its stops).
    broken = rgba_pixel(surface, 32, 32)
    assert broken[0] > 150 and broken[2] > 150
    # The unrelated shape is not polluted.
    assert rgba_pixel(surface, 96, 32) == (255, 0, 0, 255)


def test_xlink_href_is_supported():
    """The legacy ``xlink:href`` attribute resolves like ``href``."""
    defs = '''
  <linearGradient id="xlink-child" xlink:href="#common-linear"/>'''
    body = '''
    <rect x="0" y="0" width="64" height="64" fill="url(#xlink-child)"/>'''
    xlink = render(body, defs=defs)
    href = render(ICON_BODIES['plain'])
    assert pixels(xlink) == pixels(href)


def test_radial_gradient_inherits_public_stops():
    """A radial reference inherits the public stops when it defines none."""
    defs = '''
  <radialGradient id="radial-child" href="#common-radial"/>'''
    body = '''
    <rect x="0" y="0" width="64" height="64" fill="url(#radial-child)"/>'''
    surface = render(body, defs=defs)

    center = rgba_pixel(surface, 32, 32)
    corner = rgba_pixel(surface, 2, 2)
    assert center[0] > corner[0]          # white center, dark corner


def test_stored_definitions_are_not_mutated():
    """Rendering never replaces or edits the shared definition dictionary."""
    from . import defs
    from .parser import Tree
    # The resolver used to mutate the dictionary in place; it now returns a
    # fresh node. Support both names while the old one is being removed.
    resolve = getattr(defs, 'resolve_def_href',
                      getattr(defs, 'update_def_href', None))
    assert resolve is not None

    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64">
      <defs>
        <linearGradient id="base">
          <stop offset="0" stop-color="#0000ff"/>
          <stop offset="1" stop-color="#ff0000"/>
        </linearGradient>
        <linearGradient id="child" href="#base" x2="0%" y2="100%"/>
      </defs>
      <rect width="64" height="64" fill="url(#child)"/>
      <rect width="64" height="64" fill="url(#base)"/>
    </svg>'''
    tree = Tree(bytestring=svg.encode())

    def find(node, element_id):
        if node.get('id') == element_id:
            return node
        for child in node.children:
            found = find(child, element_id)
            if found is not None:
                return found
        return None

    base_node = find(tree, 'base')
    child_node = find(tree, 'child')
    assert base_node is not None and child_node is not None
    gradients = {'base': base_node, 'child': child_node}
    child_snapshot = dict(child_node)
    base_snapshot = dict(base_node)

    class DummySurface:
        tree_cache = {}

    resolved = resolve(DummySurface(), 'child', gradients)
    # A new node is returned while the dictionary keeps the original nodes.
    assert resolved is not child_node
    assert gradients['child'] is child_node
    assert gradients['base'] is base_node
    assert dict(child_node) == child_snapshot
    assert dict(base_node) == base_snapshot
