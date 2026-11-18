"""
Externally defined elements managers.

This module handles clips, gradients, masks, patterns, filters and external
nodes.

Filters
-------

Every filter primitive CairoSVG supports is evaluated as a picture, in the
local user space of the element the filter is applied to. Three coordinate
systems stay strictly separate:

* the *host* user space: the element's own ``transform`` and all ancestor
  transforms. Cairo applies it once when the filtered result is painted, so
  offsets and regions are never scaled twice;
* the *filter region*: ``filterUnits`` sets the coordinate system of the
  ``x``, ``y``, ``width`` and ``height`` attributes of ``<filter>``;
* the *primitive units*: ``primitiveUnits`` sets the coordinate system of
  every length inside a primitive, including the primitive subregion and the
  ``feOffset`` ``dx``/``dy`` values.

In ``objectBoundingBox`` units a length is a fraction (or percentage) of the
referencing element's bounding box. The two axes keep their own scale, so a
non-square object does not squash one axis, and negative values keep their
sign.

Supported primitives, evaluated in document order as a graph, are
``feOffset``, ``feFlood``, ``feBlend`` and ``feComposite`` (the six
Porter-Duff operators ``over``, ``in``, ``out``, ``atop``, ``xor`` and
``lighter``). ``feMerge``/``feMergeNode`` are handled too: nodes are painted
in document order, so the last one ends up on top, as Filter Effects 1
requires.

The standard inputs ``SourceGraphic`` and ``SourceAlpha`` are available, as
are the ``result`` values of earlier primitives. A missing ``in`` or ``in2``
uses the previous primitive's output (``SourceGraphic`` for the first
primitive); a forward or unknown ``result`` reference behaves as if the
attribute were absent, as the specification states.

Every primitive paints into its own offscreen clipped to the intersection of
the filter region and the primitive subregion. Pixels outside the region are
never painted, so a filtered element can never paint over its neighbours, and
unpainted areas of the result are transparent black.

Documented fallbacks:

* a filter with no filter primitive child at all hides the element:
  Filter Effects 1 says a filter with no primitive tree does not render its
  element, and SVG 1.1 disables elements whose filter region is empty;
* a filter region with a zero or negative width or height hides the element;
* a missing filter reference, or one that points to something which is not a
  ``<filter>`` element, is ignored and the graphic is drawn unfiltered
  (Filter Effects 1, ``filter`` property);
* ``objectBoundingBox`` units used on an object with an empty bounding box
  ignore the effect and keep the original graphic (SVG 1.1 § 7.11);
* primitives CairoSVG cannot render are skipped. The primitive before them
  stays the current input, so the rest of the chain keeps working. When no
  primitive can be rendered at all the source graphic is drawn unchanged.

None of these fallbacks move, hide or otherwise alter any other layer: the
host context is saved and restored around the whole filter, and no scratch
path, operator or clip is left behind.

"""

from .bounding_box import (
    calculate_bounding_box, is_non_empty_bounding_box, is_valid_bounding_box)
from .features import match_features
from .helpers import paint, size, transform
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

# The primitives CairoSVG knows how to evaluate.
SUPPORTED_FILTER_TAGS = frozenset((
    'feBlend', 'feComposite', 'feFlood', 'feMerge', 'feMergeNode', 'feOffset'))

# Every filter primitive element, including the ones CairoSVG cannot render.
FILTER_PRIMITIVE_TAGS = frozenset((
    'feBlend', 'feColorMatrix', 'feComponentTransfer', 'feComposite',
    'feConvolveMatrix', 'feDiffuseLighting', 'feDisplacementMap', 'feFlood',
    'feGaussianBlur', 'feImage', 'feMerge', 'feMorphology', 'feOffset',
    'feSpecularLighting', 'feTile', 'feTurbulence'))

# Standard inputs that need pictures CairoSVG does not capture. Using one
# makes the whole filter ignored, so the source graphic is kept unchanged.
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


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def _resolve_filter(surface, name):
    """Return ``(attributes, primitives)`` of the filter called ``name``.

    ``href``/``xlink:href`` references are followed, as SVG 1.1 requires:
    the referencing filter inherits the attributes it does not define, and
    the primitives of the referenced filter when it defines none itself.
    Reference loops stop at the first repeated filter. ``None`` is returned
    when the name does not reference a ``<filter>`` element.

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

    if not chain or chain[-1].tag != 'filter':
        return None

    attributes = {}
    for filter_node in reversed(chain):
        attributes.update(filter_node)

    primitives = []
    for filter_node in chain:
        children = [
            child for child in filter_node.children
            if child.tag in FILTER_PRIMITIVE_TAGS]
        if children:
            primitives = children
            break
    return attributes, primitives


def _object_length(value, default, reference):
    """Resolve one length given in object bounding box units.

    ``value`` is a fraction of ``reference`` when it is a plain number, and
    a percentage of it when it ends with ``%``. Negative numbers are kept,
    so that offsets and regions can point to either side of the box.

    """
    if value is None:
        value = default
    value = str(value).strip()
    try:
        if value.endswith('%'):
            return float(value[:-1]) * reference / 100
        return float(value) * reference
    except ValueError:
        return 0


def _user_length(surface, value, default, reference):
    """Resolve one length given in user space units.

    Percentages are relative to ``reference``; other lengths are user units
    and go through the surface DPI, as every other CairoSVG length does.

    """
    if value is None:
        value = default
    value = str(value).strip()
    try:
        if value.endswith('%'):
            return float(value[:-1]) * reference / 100
    except ValueError:
        return 0
    return size(surface, value)


def _filter_region(surface, attributes, box):
    """Return the filter region ``(x, y, width, height)`` in user space.

    ``filterUnits`` selects the coordinate system independently from
    ``primitiveUnits``. ``None`` means that ``objectBoundingBox`` units are
    used while the element has no usable bounding box: the effect is then
    ignored, as SVG 1.1 § 7.11 requires.

    """
    if attributes.get('filterUnits') == 'userSpaceOnUse':
        return (
            _user_length(
                surface, attributes.get('x'), '-10%',
                surface.context_width),
            _user_length(
                surface, attributes.get('y'), '-10%',
                surface.context_height),
            _user_length(
                surface, attributes.get('width'), '120%',
                surface.context_width),
            _user_length(
                surface, attributes.get('height'), '120%',
                surface.context_height))

    if box is None:
        return None
    box_x, box_y, box_width, box_height = box
    return (
        box_x + _object_length(attributes.get('x'), '-10%', box_width),
        box_y + _object_length(attributes.get('y'), '-10%', box_height),
        _object_length(attributes.get('width'), '120%', box_width),
        _object_length(attributes.get('height'), '120%', box_height))


def _default_subregion(region, referenced):
    """Return the default subregion of a filter primitive.

    ``x``, ``y``, ``width`` and ``height`` default to the union of the
    subregions of the referenced nodes (SVG 1.1 § 15.7.3). A standard input
    (``SourceGraphic``, ``SourceAlpha``) has the whole filter region as
    subregion, so referencing one makes the default equal to the region. A
    primitive without referenced nodes (``feFlood``) also uses the region.

    """
    if not referenced:
        return region
    min_x = min(subregion[0] for subregion in referenced)
    min_y = min(subregion[1] for subregion in referenced)
    max_x = max(subregion[0] + subregion[2] for subregion in referenced)
    max_y = max(subregion[1] + subregion[3] for subregion in referenced)
    return min_x, min_y, max_x - min_x, max_y - min_y


def _primitive_subregion(surface, attributes, child, default, box):
    """Return the filter primitive subregion ``(x, y, width, height)``.

    Every explicit attribute uses the coordinate system established by
    ``primitiveUnits``:

    * with ``userSpaceOnUse``, numbers and lengths are absolute user space
      values, and percentages follow the usual SVG rule (relative to the
      viewport);
    * with ``objectBoundingBox``, numbers and percentages are fractions of
      the element's bounding box, on two independent axes, offset by the box
      origin.

    An attribute that is not specified takes the corresponding value of
    ``default``, the union of the referenced subregions or the filter
    region. ``None`` means that object bounding box units are used on an
    object without a bounding box: the primitive produces transparent
    black, as SVG 1.1 § 7.11 requires.

    """
    if all(
            child.get(name) is None
            for name in ('x', 'y', 'width', 'height')):
        return default

    if attributes.get('primitiveUnits') == 'objectBoundingBox':
        if box is None:
            return None
        origin_x, origin_y, reference_x, reference_y = box

        def length(name, reference):
            value = child.get(name)
            return (
                None if value is None else
                _object_length(value, '0', reference))

        x = length('x', reference_x)
        y = length('y', reference_y)
        width = length('width', reference_x)
        height = length('height', reference_y)
        if x is not None:
            x += origin_x
        if y is not None:
            y += origin_y
    else:
        def length(name, reference):
            value = child.get(name)
            return (
                None if value is None else
                _user_length(surface, value, '0', reference))

        x = length('x', surface.context_width)
        y = length('y', surface.context_height)
        width = length('width', surface.context_width)
        height = length('height', surface.context_height)

    return (
        default[0] if x is None else x,
        default[1] if y is None else y,
        default[2] if width is None else width,
        default[3] if height is None else height)


def _offset_lengths(surface, attributes, child, box):
    """Return the ``(dx, dy)`` offset of an ``feOffset`` primitive.

    ``dx`` and ``dy`` use the primitive coordinate system. With
    ``objectBoundingBox`` units they are fractions of the bounding box width
    and height respectively: the two axes keep their own scale, so a
    non-square object is not squashed on a single scale, and negative values
    are honoured.

    """
    if attributes.get('primitiveUnits') == 'objectBoundingBox':
        width, height = (box[2], box[3]) if box else (0, 0)
        return (
            _object_length(child.get('dx'), '0', width),
            _object_length(child.get('dy'), '0', height))
    return (
        _user_length(
            surface, child.get('dx'), '0', surface.context_width),
        _user_length(
            surface, child.get('dy'), '0', surface.context_height))


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
    """Return a picture that paints transparent black everywhere."""
    target = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    pattern = cairo.SurfacePattern(target)
    pattern.set_extend(cairo.EXTEND_REPEAT)
    # Keep the target alive as long as the pattern is used.
    pattern._target = target
    return pattern


def _group(surface, clip, draw):
    """Paint into a Cairo group clipped to ``clip`` and return its pattern.

    The context is saved and restored around the operation, so no clip and
    no scratch path can leak into the following drawing operations. An
    empty clip yields a fully transparent picture, as a primitive outside
    its subregion produces transparent black.

    """
    if clip is not None and (clip[2] <= 0 or clip[3] <= 0):
        return _transparent_picture(surface)
    context = surface.context
    context.save()
    if clip is not None:
        context.rectangle(*clip)
        context.clip()
    context.push_group()
    draw()
    pattern = context.pop_group()
    context.restore()
    return pattern


def _paint_pattern(surface, pattern, clip, translate=None):
    """Paint ``pattern`` into a new group clipped to ``clip``."""
    def draw():
        context = surface.context
        if translate:
            context.translate(*translate)
        context.set_source(pattern)
        context.paint()

    return _group(surface, clip, draw)


def _source_alpha(surface, source):
    """Return a black picture whose alpha channel is the source's."""
    def draw():
        context = surface.context
        context.set_source(source)
        context.paint()
        context.set_operator(cairo.OPERATOR_IN)
        context.set_source_rgba(0, 0, 0, 1)
        context.paint()

    # No clip: the filter region clips results, not inputs.
    return _group(surface, None, draw)


def _merge_references(child):
    """Return the ``in`` names a primitive reads, in document order.

    ``feFlood`` has no input. ``feMerge`` reads every ``feMergeNode``; the
    other supported primitives read ``in`` and, for ``feBlend`` and
    ``feComposite``, ``in2``. Missing attributes are returned as ``None``:
    they mean "the previous primitive's output".

    """
    if child.tag == 'feFlood':
        return []
    if child.tag == 'feMerge':
        return [
            merge_node.get('in') for merge_node in child.children
            if merge_node.tag == 'feMergeNode']
    attributes = ['in']
    if child.tag in ('feBlend', 'feComposite'):
        attributes.append('in2')
    return [child.get(attribute) for attribute in attributes]


def _resolve_picture(surface, name, pictures, previous, source, region):
    """Resolve one input reference to ``(picture, subregion)``.

    ``None`` and unknown or forward ``result`` references mean the previous
    primitive's output (``SourceGraphic`` for the first primitive), as the
    specification requires. Standard inputs have the filter region as their
    subregion; results keep the subregion they were computed with.

    """
    if name == 'SourceGraphic':
        return source, region
    if name == 'SourceAlpha':
        if 'SourceAlpha' not in pictures:
            pictures['SourceAlpha'] = (_source_alpha(surface, source), region)
        return pictures['SourceAlpha']
    if name is not None and name in pictures:
        return pictures[name]
    if previous is not None:
        return previous
    return source, region


def _apply_primitive(surface, attributes, child, clip, inputs, box):
    """Evaluate one filter primitive from its resolved ``inputs``."""
    def first():
        return inputs[0][0]

    if child.tag == 'feOffset':
        dx, dy = _offset_lengths(surface, attributes, child, box)
        return _paint_pattern(surface, first(), clip, translate=(dx, dy))

    if child.tag == 'feFlood':
        color = surface.map_color(
            paint(child.get('flood-color'))[1] or 'black',
            float(child.get('flood-opacity', 1)))

        def draw():
            surface.context.set_source_rgba(*color)
            surface.context.paint()

        return _group(surface, clip, draw)

    if child.tag == 'feMerge':
        def draw():
            for picture, _ in inputs:
                surface.context.set_operator(cairo.OPERATOR_OVER)
                surface.context.set_source(picture)
                surface.context.paint()

        return _group(surface, clip, draw)

    if child.tag in ('feBlend', 'feComposite'):
        backdrop = inputs[1][0]
        if child.tag == 'feBlend':
            key = child.get('mode', 'normal')
            operator = BLEND_OPERATORS.get(key, BLEND_OPERATORS['normal'])
        else:
            key = child.get('operator', 'over')
            operator = COMPOSITE_OPERATORS.get(
                key, COMPOSITE_OPERATORS['over'])

        def draw():
            surface.context.set_source(backdrop)
            surface.context.paint()
            surface.context.set_operator(operator)
            surface.context.set_source(first())
            surface.context.paint()

        return _group(surface, clip, draw)

    return _transparent_picture(surface)


def _supported_primitives(primitives):
    """Return the primitives CairoSVG can evaluate, in document order.

    Unsupported primitives (``feGaussianBlur`` and friends) are left out.
    A ``result`` produced by one of them does not exist, so later references
    behave as if the attribute were absent, and the previous rendered
    result stays the current input, as for an unknown result.

    """
    return [
        child for child in primitives if child.tag in SUPPORTED_FILTER_TAGS]


def _has_unsupported_input(primitives):
    """Tell whether a filter needs a standard input CairoSVG cannot capture.

    ``BackgroundImage``, ``BackgroundAlpha``, ``FillPaint`` and
    ``StrokePaint`` would need pictures CairoSVG does not keep. A filter
    using one of them is ignored as a whole, and the source graphic is
    drawn unchanged.

    """
    for child in primitives:
        for reference in _merge_references(child):
            if reference in UNSUPPORTED_FILTER_INPUTS:
                return True
    return False


def apply_filter(surface, node, name, source):
    """Apply the filter ``name`` to ``source``, the ``node``'s own picture.

    ``source`` is a Cairo pattern in the user space of ``node``. That user
    space already contains the element's ``transform`` and all ancestor
    transforms, so the host transform is applied exactly once, when the
    result is painted, and filter lengths are resolved in the same space as
    the element.

    The result is a picture clipped to the filter region. Return values:

    * a Cairo pattern to paint instead of the source;
    * ``EMPTY_FILTER`` when the element must not be rendered at all: an
      empty filter, or a filter region with a zero or negative width or
      height;
    * ``None`` when the filter must be ignored: an unknown filter, an
      unsupported standard input, or object bounding box units on an object
      without a bounding box. The caller then paints the unfiltered source,
      so the graphic is never lost and no other layer is changed.

    """
    if 'id' in node and node['id'] in surface.masks:
        # Masks are rendered into their own surface; never filter them here.
        return None

    resolved = _resolve_filter(surface, name)
    if resolved is None:
        return None
    attributes, primitives = resolved

    if not primitives:
        # An empty filter has no filter primitive tree: the element is not
        # rendered (Filter Effects 1 § 9.3; SVG 1.1 disables the element).
        return EMPTY_FILTER
    if _has_unsupported_input(primitives):
        return None
    primitives = _supported_primitives(primitives)
    if not primitives:
        # Nothing can be evaluated: keep the original graphic untouched.
        return None

    box = calculate_bounding_box(surface, node)
    if not is_valid_bounding_box(box) or 0 in box[2:]:
        box = None

    region = _filter_region(surface, attributes, box)
    if region is None:
        # objectBoundingBox units on an object without a bounding box.
        return None
    if region[2] <= 0 or region[3] <= 0:
        return EMPTY_FILTER

    # ``pictures`` maps a ``result`` name to ``(picture, subregion)``.
    # ``previous`` is the previous primitive's output, and its subregion
    # feeds the default subregion of the next primitive.
    pictures = {}
    previous = None
    for child in primitives:
        references = _merge_references(child)
        resolved_inputs = [
            _resolve_picture(
                surface, reference, pictures, previous, source, region)
            for reference in references]
        if not references:
            # feFlood has no referenced node.
            referenced_subregions = []
        else:
            referenced_subregions = [
                subregion for _, subregion in resolved_inputs]
        default = _default_subregion(region, referenced_subregions)
        subregion = _primitive_subregion(
            surface, attributes, child, default, box)
        if subregion is None:
            # objectBoundingBox units without a bounding box: disable the
            # primitive, its result is transparent black.
            subregion = (region[0], region[1], 0, 0)
        clip = _intersect(region, subregion)
        picture = _apply_primitive(
            surface, attributes, child, clip, resolved_inputs, box)
        if child.get('result') is not None:
            # The closest preceding primitive wins for a duplicated name.
            pictures[child.get('result')] = (picture, subregion)
        previous = (picture, subregion)

    return _paint_pattern(surface, previous[0], region)


def use(surface, node):
    """Draw the content of another SVG node."""
    surface.reference_count += 1
    if not node.unsafe and surface.reference_count > 100_000:
        raise ValueError(
            'Abort rendering: more than 100\u00a0000 referenced elements')
    surface.context.save()
    surface.context.translate(
        size(surface, node.get('x'), 'x'), size(surface, node.get('y'), 'y'))
    # The attributes are only hidden while the referenced tree is drawn:
    # ``draw`` needs them again afterwards to compute the filter region of
    # the ``use`` element, whose content is shifted by ``x`` and ``y``.
    hidden = {}
    for attribute in ('x', 'y', 'viewBox', 'mask'):
        if attribute in node:
            hidden[attribute] = node.pop(attribute)
    href = parse_url(node.get_href()).geturl()
    try:
        tree = Tree(
            url=href, url_fetcher=node.url_fetcher, parent=node,
            tree_cache=surface.tree_cache, unsafe=node.unsafe)
    except TypeError:
        node.update(hidden)
        surface.context.restore()
        return

    if not match_features(tree.xml_tree):
        node.update(hidden)
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
    node.update(hidden)
    surface.context.restore()
