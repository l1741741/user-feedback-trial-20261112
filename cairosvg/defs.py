"""
Externally defined elements managers.

This module handles clips, gradients, masks, patterns, filters and external
nodes.

Filters
-------

The SVG filter pipeline is implemented with Cairo groups, in the user space
of the element the filter is applied to. This keeps three coordinate systems
independent from each other:

* the *host* coordinate system (the element's ``transform`` and all ancestor
  transforms), which Cairo applies exactly once when the result is painted;
* the *filter region* coordinate system, defined by ``filterUnits`` and the
  filter's ``x``, ``y``, ``width`` and ``height`` attributes;
* the *primitive* coordinate system, defined by ``primitiveUnits`` and used
  for every length inside a filter primitive.

Supported primitives are ``feOffset``, ``feFlood``, ``feBlend``,
``feComposite`` (``over``, ``in``, ``out``, ``atop``, ``xor`` and
``lighter``) and ``feMerge``/``feMergeNode``.  They are evaluated in document
order as a graph: a primitive takes its ``in`` (and ``in2`` for ``feBlend``
and ``feComposite``) from ``SourceGraphic``, ``SourceAlpha``, a previous
``result`` attribute, or the previous primitive's output when the attribute
is absent (references to missing or forward results behave as if the attribute
were absent). ``feMerge`` paints its input list in order, bottom to top.

Every primitive paints into a group clipped to the intersection of the filter
region and the primitive subregion, so pixels outside the declared region are
never painted and the group is left transparent (Cairo never leaks the
scratch path into later drawing operations).

Fallbacks follow the documented CairoSVG contract:

* an empty filter makes the element disappear (the specification describes
  the result as transparent black);
* a filter region with a zero or negative width or height disables the
  element it is applied to;
* primitives CairoSVG cannot render (``feGaussianBlur`` and friends) are
  skipped; an unsupported standard input (``BackgroundImage``,
  ``BackgroundAlpha``, ``FillPaint``, ``StrokePaint``) makes the whole filter
  ignored, and the unfiltered source graphic is drawn unchanged.

"""

from .bounding_box import (
    calculate_bounding_box, is_non_empty_bounding_box, is_valid_bounding_box)
from .features import match_features
from .helpers import paint as paint_color, size, transform
from .parser import Tree
from .surface import cairo
from .url import parse_url

BLEND_OPERATORS = {
    'darken': cairo.OPERATOR_DARKEN,
    'lighten': cairo.OPERATOR_LIGHTEN,
    'multiply': cairo.OPERATOR_MULTIPLY,
    'normal': cairo.OPERATOR_OVER,
    'screen': cairo.OPERATOR_SCREEN,
}

COMPOSITE_OPERATORS = {
    'atop': cairo.OPERATOR_ATOP,
    'in': cairo.OPERATOR_IN,
    'lighter': cairo.OPERATOR_ADD,
    'out': cairo.OPERATOR_OUT,
    'over': cairo.OPERATOR_OVER,
    'xor': cairo.OPERATOR_XOR,
}

SUPPORTED_FILTER_TAGS = frozenset((
    'feBlend', 'feComposite', 'feFlood', 'feMerge', 'feMergeNode', 'feOffset'))

# All filter primitive elements, including the ones CairoSVG does not render.
ALL_FILTER_PRIMITIVES = frozenset((
    'feBlend', 'feColorMatrix', 'feComponentTransfer', 'feComposite',
    'feConvolveMatrix', 'feDiffuseLighting', 'feDisplacementMap', 'feFlood',
    'feGaussianBlur', 'feImage', 'feMerge', 'feMorphology', 'feOffset',
    'feSpecularLighting', 'feTile', 'feTurbulence'))

# Pseudo inputs that would need pictures we cannot capture.
UNSUPPORTED_FILTER_INPUTS = frozenset((
    'BackgroundAlpha', 'BackgroundImage', 'FillPaint', 'StrokePaint'))

# Return value of ``apply_filter`` when the element must not be rendered.
EMPTY_FILTER = object()

EXTEND_OPERATORS = {
    'none': cairo.EXTEND_NONE,
    'pad': cairo.EXTEND_PAD,
    'reflect': cairo.EXTEND_REFLECT,
    'repeat': cairo.EXTEND_REPEAT,
}


def update_def_href(surface, def_name, def_dict, chain=None):
    """Update the attributes of the def according to its href attribute."""
    def_node = def_dict[def_name]
    href = parse_url(def_node.get_href()).fragment
    if chain is None:
        chain = set()
    if href in def_dict and href not in chain:
        chain.add(href)
        update_def_href(surface, href, def_dict, chain)
        href_node = def_dict[href]
        def_dict[def_name] = Tree(
            url=f'#{def_name}', url_fetcher=def_node.url_fetcher,
            parent=href_node, parent_children=(not def_node.children),
            tree_cache=surface.tree_cache, unsafe=def_node.unsafe)
        # Inherit attributes generally not inherited
        for key, value in href_node.items():
            if key not in def_dict[def_name]:
                def_dict[def_name][key] = value


def parse_all_defs(surface, node):
    """Recursively visit all child nodes and process definition elements."""

    # Handle node
    parse_def(surface, node)

    # Visit all children recursively
    if node.children:
        for child in node.children:
            parse_all_defs(surface, child)


def parse_def(surface, node):
    """Parse the SVG definitions."""
    for def_type in (
            'marker', 'gradient', 'pattern', 'path', 'mask', 'filter',
            'image'):
        if def_type in node.tag.lower() and 'id' in node:
            getattr(surface, f'{def_type}s')[node['id']] = node


def gradient_or_pattern(surface, node, name, opacity):
    """Gradient or pattern color."""
    if name in surface.gradients:
        update_def_href(surface, name, surface.gradients)
        return draw_gradient(surface, node, name, opacity)
    elif name in surface.patterns:
        update_def_href(surface, name, surface.patterns)
        return draw_pattern(surface, node, name, opacity)


def marker(surface, node):
    """Store a marker definition."""
    parse_def(surface, node)


def mask(surface, node):
    """Store a mask definition."""
    parse_def(surface, node)


def filter_(surface, node):
    """Store a filter definition."""
    parse_def(surface, node)


def linear_gradient(surface, node):
    """Store a linear gradient definition."""
    parse_def(surface, node)


def radial_gradient(surface, node):
    """Store a radial gradient definition."""
    parse_def(surface, node)


def pattern(surface, node):
    """Store a pattern definition."""
    parse_def(surface, node)


def clip_path(surface, node):
    """Store a clip path definition."""
    if 'id' in node:
        surface.paths[node['id']] = node


def paint_mask(surface, node, name, opacity):
    """Paint the mask of the current surface."""
    mask_node = surface.masks[name]
    mask_node.tag = 'g'
    mask_node['opacity'] = opacity

    if mask_node.get('maskUnits') == 'userSpaceOnUse':
        width_ref, height_ref = 'x', 'y'
    else:
        x = size(surface, node.get('x'), 'x')
        y = size(surface, node.get('y'), 'y')
        width = size(surface, node.get('width'), 'x')
        height = size(surface, node.get('height'), 'y')
        width_ref = width or surface.width
        height_ref = height or surface.height

    mask_node['x'] = size(surface, mask_node.get('x', '-10%'), width_ref)
    mask_node['y'] = size(surface, mask_node.get('y', '-10%'), height_ref)
    mask_node['height'] = size(
        surface, mask_node.get('height', '120%'), height_ref)
    mask_node['width'] = size(
        surface, mask_node.get('width', '120%'), width_ref)

    if mask_node.get('maskUnits') == 'userSpaceOnUse':
        x = mask_node['x']
        y = mask_node['y']
        mask_node['viewBox'] = '{x} {y} {width} {height}'.format(**mask_node)

    from .surface import SVGSurface  # circular import
    mask_surface = SVGSurface(mask_node, None, surface.dpi, surface)
    surface.context.save()
    surface.context.translate(x, y)
    surface.context.scale(
        mask_node['width'] / mask_surface.width,
        mask_node['height'] / mask_surface.height)
    surface.context.mask_surface(mask_surface.cairo)
    surface.context.restore()


def draw_gradient(surface, node, name, opacity):
    """Gradients colors."""
    gradient_node = surface.gradients[name]

    if gradient_node.get('gradientUnits') == 'userSpaceOnUse':
        width_ref, height_ref = 'x', 'y'
        diagonal_ref = 'xy'
    else:
        bounding_box = calculate_bounding_box(surface, node)
        if not is_non_empty_bounding_box(bounding_box):
            return False
        x = size(surface, bounding_box[0], 'x')
        y = size(surface, bounding_box[1], 'y')
        width = size(surface, bounding_box[2], 'x')
        height = size(surface, bounding_box[3], 'y')
        width_ref = height_ref = diagonal_ref = 1

    if gradient_node.tag == 'linearGradient':
        x1 = size(surface, gradient_node.get('x1', '0%'), width_ref)
        x2 = size(surface, gradient_node.get('x2', '100%'), width_ref)
        y1 = size(surface, gradient_node.get('y1', '0%'), height_ref)
        y2 = size(surface, gradient_node.get('y2', '0%'), height_ref)
        gradient_pattern = cairo.LinearGradient(x1, y1, x2, y2)

    elif gradient_node.tag == 'radialGradient':
        r = size(surface, gradient_node.get('r', '50%'), diagonal_ref)
        cx = size(surface, gradient_node.get('cx', '50%'), width_ref)
        cy = size(surface, gradient_node.get('cy', '50%'), height_ref)
        fx = size(surface, gradient_node.get('fx', str(cx)), width_ref)
        fy = size(surface, gradient_node.get('fy', str(cy)), height_ref)
        gradient_pattern = cairo.RadialGradient(fx, fy, 0, cx, cy, r)

    else:
        return False

    # Apply matrix to set coordinate system for gradient
    if gradient_node.get('gradientUnits') != 'userSpaceOnUse':
        gradient_pattern.set_matrix(cairo.Matrix(
            1 / width, 0, 0, 1 / height, - x / width, - y / height))

    # Apply transform of gradient
    transform(
        surface, gradient_node.get('gradientTransform'), gradient_pattern)

    # Apply gradient (<stop> by <stop>)
    offset = 0
    for child in gradient_node.children:
        offset = max(offset, size(surface, child.get('offset'), 1))
        stop_color = surface.map_color(
            child.get('stop-color', 'black'),
            float(child.get('stop-opacity', 1)) * opacity)
        gradient_pattern.add_color_stop_rgba(offset, *stop_color)

    # Set spread method for gradient outside target bounds
    gradient_pattern.set_extend(EXTEND_OPERATORS.get(
        gradient_node.get('spreadMethod', 'pad'), EXTEND_OPERATORS['pad']))

    surface.context.set_source(gradient_pattern)
    return True


def draw_pattern(surface, node, name, opacity):
    """Draw a pattern image."""
    pattern_node = surface.patterns[name]
    pattern_node['opacity'] = float(pattern_node.get('opacity', 1)) * opacity
    pattern_node.tag = 'g'
    transform(surface, pattern_node.get('patternTransform'))

    if pattern_node.get('viewBox'):
        if not (size(surface, pattern_node.get('width', 1), 1) and
                size(surface, pattern_node.get('height', 1), 1)):
            return False
    else:
        if not (size(surface, pattern_node.get('width', 0), 1) and
                size(surface, pattern_node.get('height', 0), 1)):
            return False

    if pattern_node.get('patternUnits') == 'userSpaceOnUse':
        x = size(surface, pattern_node.get('x'), 'x')
        y = size(surface, pattern_node.get('y'), 'y')
        pattern_width = size(surface, pattern_node.get('width', 0), 1)
        pattern_height = size(surface, pattern_node.get('height', 0), 1)
    else:
        _, _, width, height = calculate_bounding_box(surface, node)
        x = size(surface, pattern_node.get('x'), 1) * width
        y = size(surface, pattern_node.get('y'), 1) * height
        pattern_width = (
            size(surface, pattern_node.pop('width', '1'), 1) * width)
        pattern_height = (
            size(surface, pattern_node.pop('height', '1'), 1) * height)
        if 'viewBox' not in pattern_node:
            pattern_node['width'] = pattern_width
            pattern_node['height'] = pattern_height
            if pattern_node.get('patternContentUnits') == 'objectBoundingBox':
                pattern_node['transform'] = f'scale({width}, {height})'

    # Fail if pattern has an invalid size
    if pattern_width == 0.0 or pattern_height == 0.0:
        return False

    from .surface import SVGSurface  # circular import
    pattern_surface = SVGSurface(pattern_node, None, surface.dpi, surface)
    pattern_pattern = cairo.SurfacePattern(pattern_surface.cairo)
    pattern_pattern.set_extend(cairo.EXTEND_REPEAT)
    pattern_pattern.set_matrix(cairo.Matrix(
        pattern_surface.width / pattern_width, 0, 0,
        pattern_surface.height / pattern_height, -x, -y))
    surface.context.set_source(pattern_pattern)
    return True


def _resolve_filter(surface, name):
    """Return ``(attributes, children)`` for the filter called ``name``.

    ``xlink:href``/``href`` references between filters are followed: the
    referencing filter inherits the attributes it does not define, and the
    primitives of the referenced filter when it defines none itself, as the
    SVG specification requires. Reference cycles stop at the first repeat.
    ``None`` is returned when the filter does not exist.

    """
    if name not in surface.filters:
        return None

    chain = []
    seen = set()
    current = name
    while (
            current is not None and current in surface.filters and
            current not in seen):
        seen.add(current)
        filter_node = surface.filters[current]
        chain.append(filter_node)
        current = parse_url(filter_node.get_href()).fragment or None

    attributes = {}
    for filter_node in reversed(chain):
        attributes.update(filter_node)

    children = []
    for filter_node in chain:
        primitives = [
            child for child in filter_node.children
            if child.tag in ALL_FILTER_PRIMITIVES]
        if primitives:
            children = primitives
            break
    return attributes, children


def _object_length(value, default, reference):
    """Resolve a length in object bounding box units.

    Numbers and percentages are both fractions (or hundredths) of
    ``reference``, the bounding box width or height.

    """
    if value is None:
        value = default
    value = str(value).strip()
    if value.endswith('%'):
        return float(value[:-1]) * reference / 100
    try:
        return float(value) * reference
    except ValueError:
        return 0


def _user_length(surface, value, default, reference):
    """Resolve a user space filter length.

    Percentages are relative to ``reference``. Plain numbers and lengths with
    units are user units, resolved with the surface DPI.

    """
    if value is None:
        value = default
    value = str(value).strip()
    if value.endswith('%'):
        return float(value[:-1]) * reference / 100
    return size(surface, value)


def _filter_region(surface, attributes, box):
    """Return ``(x, y, width, height)`` of the filter region, in user space.

    ``filterUnits`` selects the coordinate system of the region, independently
    from the primitive coordinate system. ``None`` means that the region uses
    object bounding box units while the element has no usable bounding box:
    the effect is then ignored, as required for object bounding box units.

    """
    if attributes.get('filterUnits') == 'userSpaceOnUse':
        width = _user_length(
            surface, attributes.get('width'), '120%', surface.context_width)
        height = _user_length(
            surface, attributes.get('height'), '120%', surface.context_height)
        x = _user_length(
            surface, attributes.get('x'), '-10%', surface.context_width)
        y = _user_length(
            surface, attributes.get('y'), '-10%', surface.context_height)
        return x, y, width, height

    if box is None:
        return None
    box_x, box_y, box_width, box_height = box
    x = _object_length(attributes.get('x'), '-10%', box_width)
    y = _object_length(attributes.get('y'), '-10%', box_height)
    width = _object_length(attributes.get('width'), '120%', box_width)
    height = _object_length(attributes.get('height'), '120%', box_height)
    return box_x + x, box_y + y, width, height


def _subregion_length(surface, value, default, origin, reference):
    """Resolve one coordinate of a filter primitive subregion.

    With user space units, absolute lengths are user space coordinates and
    percentages are relative to the filter region (``origin`` is the region
    position and ``reference`` its size). The default subregion is
    ``0%, 0%, 100%, 100%`` of the region.

    """
    if value is None:
        value = default
    value = str(value).strip()
    if value.endswith('%'):
        return origin + float(value[:-1]) * reference / 100
    return size(surface, value)


def _primitive_subregion(surface, attributes, child, region, box):
    """Return the clipping rectangle of a filter primitive, in user space.

    The default subregion is the filter region, as stated by the
    specification. Explicit values use the primitive coordinate system:
    with ``primitiveUnits="objectBoundingBox"`` numbers and percentages are
    fractions of the element bounding box, otherwise they are user space
    lengths and percentages are relative to the filter region. ``None``
    means that the primitive is disabled: a subregion with a zero or
    negative width or height yields transparent black.

    """
    if all(
            child.get(name) is None
            for name in ('x', 'y', 'width', 'height')):
        return region

    if attributes.get('primitiveUnits') == 'objectBoundingBox':
        if box is None:
            return None
        origin_x, origin_y = box[0], box[1]
        reference_x, reference_y = box[2], box[3]
        return (
            origin_x + _object_length(child.get('x'), '0%', reference_x),
            origin_y + _object_length(child.get('y'), '0%', reference_y),
            _object_length(child.get('width'), '100%', reference_x),
            _object_length(child.get('height'), '100%', reference_y))

    region_x, region_y, region_width, region_height = region
    return (
        _subregion_length(
            surface, child.get('x'), '0%', region_x, region_width),
        _subregion_length(
            surface, child.get('y'), '0%', region_y, region_height),
        _subregion_length(
            surface, child.get('width'), '100%', 0, region_width),
        _subregion_length(
            surface, child.get('height'), '100%', 0, region_height))


def _offset_lengths(surface, attributes, child, box):
    """Return the ``(dx, dy)`` offset of an ``feOffset`` primitive.

    ``dx`` and ``dy`` live in the primitive coordinate system. With
    ``objectBoundingBox`` units, horizontal and vertical offsets are
    fractions of the bounding box width and height respectively: the two axes
    are kept separate, so a non-square object is not squashed onto one scale,
    and the sign of the offset is preserved.

    """
    if attributes.get('primitiveUnits') == 'objectBoundingBox':
        width, height = (box[2], box[3]) if box else (0, 0)
        return (
            _object_length(child.get('dx'), '0', width),
            _object_length(child.get('dy'), '0', height))
    return (
        _user_length(surface, child.get('dx'), '0', surface.context_width),
        _user_length(surface, child.get('dy'), '0', surface.context_height))


def _intersect(first, second):
    """Intersect two ``(x, y, width, height)`` rectangles."""
    if first is None:
        return second
    if second is None:
        return first
    x = max(first[0], second[0])
    y = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    return x, y, max(0, right - x), max(0, bottom - y)


def _transparent_picture(surface):
    """Return a pattern painting transparent black everywhere."""
    target = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    pattern = cairo.SurfacePattern(target)
    pattern.set_extend(cairo.EXTEND_REPEAT)
    # Keep the target alive as long as the pattern is used.
    pattern._target = target
    return pattern


def _group(surface, clip, paint):
    """Run ``paint`` in a Cairo group clipped to ``clip``.

    The context is restored to its previous state and left without a current
    path, so no scratch path can leak into later drawing operations. The
    returned pattern may be painted several times. An empty clip yields a
    fully transparent picture, as filter primitives outside their subregion
    produce transparent black.

    """
    if clip is not None and (clip[2] <= 0 or clip[3] <= 0):
        return _transparent_picture(surface)
    context = surface.context
    context.save()
    if clip is not None:
        context.rectangle(*clip)
        context.clip()
    context.push_group()
    paint()
    pattern = context.pop_group()
    context.restore()
    return pattern


def _paint_pattern(surface, pattern, clip, translate=None):
    """Paint ``pattern`` into a new group clipped to ``clip``."""
    def paint():
        if translate:
            surface.context.translate(*translate)
        surface.context.set_source(pattern)
        surface.context.paint()

    return _group(surface, clip, paint)


def _paint_alpha(surface, source):
    """Paint black with the alpha channel of ``source``."""
    surface.context.set_source(source)
    surface.context.paint()
    surface.context.set_operator(cairo.OPERATOR_IN)
    surface.context.set_source_rgba(0, 0, 0, 1)
    surface.context.paint()


def _resolve_input(surface, child, attribute, pictures, previous, source):
    """Resolve an ``in``/``in2`` attribute of a filter primitive.

    A missing attribute, or a reference to a result that does not appear
    before the primitive, means the previous primitive's output, or
    ``SourceGraphic`` for the first primitive. ``SourceAlpha`` is computed
    lazily and cached.

    """
    name = child.get(attribute)
    if name is None:
        return previous if previous is not None else source
    if name == 'SourceGraphic':
        return source
    if name == 'SourceAlpha':
        if '__source_alpha__' not in pictures:
            region = pictures.get('__region__')
            pictures['__source_alpha__'] = _group(
                surface, region, lambda: _paint_alpha(surface, source))
        return pictures['__source_alpha__']
    if name in pictures:
        return pictures[name]
    # Forward and non-existent references: as if the attribute was not set.
    return previous if previous is not None else source


def _apply_primitive(
        surface, attributes, child, clip, pictures, previous, source, box):
    """Evaluate one filter primitive, in document order, and return it."""
    def input_(name='in'):
        return _resolve_input(
            surface, child, name, pictures, previous, source)

    if child.tag == 'feOffset':
        dx, dy = _offset_lengths(surface, attributes, child, box)
        return _paint_pattern(surface, input_(), clip, translate=(dx, dy))

    if child.tag == 'feFlood':
        color = surface.map_color(
            paint_color(child.get('flood-color'))[1] or 'black',
            float(child.get('flood-opacity', 1)))

        def paint():
            surface.context.set_source_rgba(*color)
            surface.context.paint()

        return _group(surface, clip, paint)

    if child.tag == 'feMerge':
        merge_inputs = [
            _resolve_input(
                surface, merge_node, 'in', pictures, previous, source)
            for merge_node in child.children
            if merge_node.tag == 'feMergeNode']

        def paint():
            for picture in merge_inputs:
                surface.context.set_operator(cairo.OPERATOR_OVER)
                surface.context.set_source(picture)
                surface.context.paint()

        return _group(surface, clip, paint)

    if child.tag == 'feBlend':
        source_picture = input_()
        backdrop = input_('in2')
        operator = BLEND_OPERATORS.get(
            child.get('mode', 'normal'), BLEND_OPERATORS['normal'])

        def paint():
            surface.context.set_source(backdrop)
            surface.context.paint()
            surface.context.set_operator(operator)
            surface.context.set_source(source_picture)
            surface.context.paint()

        return _group(surface, clip, paint)

    if child.tag == 'feComposite':
        source_picture = input_()
        backdrop = input_('in2')
        operator = COMPOSITE_OPERATORS.get(
            child.get('operator', 'over'), COMPOSITE_OPERATORS['over'])

        def paint():
            surface.context.set_source(backdrop)
            surface.context.paint()
            surface.context.set_operator(operator)
            surface.context.set_source(source_picture)
            surface.context.paint()

        return _group(surface, clip, paint)

    return _transparent_picture(surface)


def _has_unsupported_input(children):
    """Tell whether a filter needs a standard input we cannot capture.

    ``BackgroundImage``, ``BackgroundAlpha``, ``FillPaint`` and
    ``StrokePaint`` need pictures CairoSVG does not keep; a filter using one
    of them is ignored as a whole.

    """
    for child in children:
        for attribute in ('in', 'in2'):
            if child.get(attribute) in UNSUPPORTED_FILTER_INPUTS:
                return True
        for merge_node in child.children:
            if merge_node.get('in') in UNSUPPORTED_FILTER_INPUTS:
                return True
    return False


def _supported_primitives(children):
    """Return the primitives CairoSVG can evaluate, in document order.

    Unsupported primitives (``feGaussianBlur`` and friends) are left out:
    CairoSVG documents that only ``feOffset``, ``feBlend`` and ``feFlood``
    are supported. A ``result`` produced by an unsupported primitive does not
    exist, so later references to it behave as if the attribute was absent,
    and the previous rendered result is used, as the specification requires
    for unknown results.

    """
    return [
        child for child in children if child.tag in SUPPORTED_FILTER_TAGS]


def apply_filter(surface, node, name, source):
    """Apply the filter ``name`` to ``source``, the ``node``'s own picture.

    ``source`` is a Cairo pattern painted in the user space of ``node``. That
    user space already contains the element's own ``transform`` and all the
    ancestor transforms, so filter lengths are resolved in the same space as
    the element and the host transform is applied exactly once, when the
    result is painted.

    Primitives are evaluated in document order, each one painting into a
    group clipped to its subregion, so nothing is painted outside the
    declared filter region.

    Return values:

    * a Cairo pattern, to paint instead of the source;
    * ``EMPTY_FILTER`` when the element must not be rendered at all: a filter
      with no primitive produces transparent black, and a filter region with
      a zero width or height disables the element (SVG 1.1, Filter Effects 1);
    * ``None`` when the filter must be ignored: a non-existent filter, an
      unsupported primitive or standard input, or object bounding box units
      without a bounding box. The caller then paints the unfiltered source, so
      the graphic is never lost and later layers never move.

    """
    if 'id' in node and node['id'] in surface.masks:
        # Masks are rendered into their own surface; never filter them here.
        return None

    resolved = _resolve_filter(surface, name)
    if resolved is None:
        # Filter Effects 1: a reference to a non-existent object makes the
        # whole filter chain ignored, so the graphic is kept unfiltered.
        return None
    attributes, children = resolved
    if not children:
        # SVG 1.1 and Filter Effects 1: an empty filter produces transparent
        # black, so the element is not rendered.
        return EMPTY_FILTER
    if _has_unsupported_input(children):
        return None
    children = _supported_primitives(children)
    if not children:
        # Nothing can be evaluated: keep the original graphic untouched.
        return None

    box = calculate_bounding_box(surface, node)
    if not is_valid_bounding_box(box) or 0 in box[2:]:
        box = None

    region = _filter_region(surface, attributes, box)
    if region is None:
        return None
    if region[2] <= 0 or region[3] <= 0:
        return EMPTY_FILTER

    pictures = {'__region__': region}
    previous = None
    for child in children:
        subregion = _primitive_subregion(
            surface, attributes, child, region, box)
        clip = _intersect(region, subregion)
        picture = _apply_primitive(
            surface, attributes, child, clip, pictures, previous, source, box)
        if child.get('result') is not None:
            pictures[child.get('result')] = picture
        previous = picture

    return _paint_pattern(surface, previous, region)


def use(surface, node):
    """Draw the content of another SVG node."""
    surface.reference_count += 1
    if not node.unsafe and surface.reference_count > 100_000:
        raise ValueError(
            'Abort rendering: more than 100 000 referenced elements')
    surface.context.save()
    surface.context.translate(
        size(surface, node.get('x'), 'x'), size(surface, node.get('y'), 'y'))
    if 'x' in node:
        del node['x']
    if 'y' in node:
        del node['y']
    if 'viewBox' in node:
        del node['viewBox']
    if 'mask' in node:
        del node['mask']
    href = parse_url(node.get_href()).geturl()
    try:
        tree = Tree(
            url=href, url_fetcher=node.url_fetcher, parent=node,
            tree_cache=surface.tree_cache, unsafe=node.unsafe)
    except TypeError:
        surface.context.restore()
        return

    if not match_features(tree.xml_tree):
        surface.context.restore()
        return

    if tree.tag in ('svg', 'symbol'):
        # Explicitely specified
        # http://www.w3.org/TR/SVG11/struct.html#UseElement
        tree.tag = 'svg'
        if 'width' in node and 'height' in node:
            tree['width'], tree['height'] = node['width'], node['height']

    surface.draw(tree)
    node.get('fill', None)
    node.get('stroke', None)
    surface.context.restore()
