"""
Externally defined elements managers.

This module handles clips, gradients, masks, patterns and external nodes.

"""

from .bounding_box import calculate_bounding_box, is_non_empty_bounding_box
from .features import match_features
from .helpers import paint, parse_transform, size, transform
from .parser import Node, Tree
from .shapes import rect
from .surface import cairo
from .url import parse_url

BLEND_OPERATORS = {
    'darken': cairo.OPERATOR_DARKEN,
    'lighten': cairo.OPERATOR_LIGHTEN,
    'multiply': cairo.OPERATOR_MULTIPLY,
    'normal': cairo.OPERATOR_OVER,
    'screen': cairo.OPERATOR_SCREEN,
}

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

    # Work on a copy: a pattern can be referenced many times, with different
    # opacities and from shapes with different bounding boxes. The definition
    # stored in ``surface.patterns`` must never be mutated by a reference.
    # Children keep the working copy as parent so that attributes set below
    # (viewport size, content transform, opacity) are inherited as expected.
    pattern_children = pattern_node.children
    # Attributes resolved through href inheritance are stored on the wrapper
    # node rather than on its XML element; snapshot them before rebuilding.
    pattern_attributes = dict(pattern_node)
    pattern_node = Node(
        pattern_node.element, pattern_node.style,
        pattern_node.url_fetcher, parent=pattern_node.parent,
        unsafe=pattern_node.unsafe)
    pattern_node.update(pattern_attributes)
    pattern_node.tag = 'g'
    pattern_node.children = [
        Node(child.element, child.style, child.url_fetcher,
             parent=pattern_node, unsafe=child.unsafe)
        for child in pattern_children]

    user_space = pattern_node.get('patternUnits') == 'userSpaceOnUse'
    box_width = box_height = None
    if user_space:
        x = size(surface, pattern_node.get('x'), 'x')
        y = size(surface, pattern_node.get('y'), 'y')
        pattern_width = size(surface, pattern_node.get('width', 0), 1)
        pattern_height = size(surface, pattern_node.get('height', 0), 1)
    else:
        # Pattern geometry follows the bounding box of the painted object, so
        # a given definition adapts to every shape it is referenced from. The
        # tile origin is the bounding box origin, x/y being offsets from it.
        bounding_box = calculate_bounding_box(surface, node)
        if not is_non_empty_bounding_box(bounding_box):
            return False
        box_x, box_y, box_width, box_height = bounding_box
        x = box_x + size(surface, pattern_node.get('x'), 1) * box_width
        y = box_y + size(surface, pattern_node.get('y'), 1) * box_height
        pattern_width = size(
            surface, pattern_node.get('width', 0), 1) * box_width
        pattern_height = size(
            surface, pattern_node.get('height', 0), 1) * box_height

    # A tile with no usable area disables the paint server instead of leaking
    # the previous source or asking cairo for a degenerate surface.
    if pattern_width <= 0.0 or pattern_height <= 0.0:
        return False

    # Pattern contents live in their own coordinate system, independent from
    # the one used to place and size the tiles.
    if 'viewBox' not in pattern_node:
        # Give the nested surface an explicit viewport matching the tile.
        pattern_node['width'] = pattern_width
        pattern_node['height'] = pattern_height
        if (pattern_node.get('patternContentUnits') == 'objectBoundingBox'
                and box_width is not None):
            # Contents are expressed in fractions of the object bounding box.
            pattern_node['transform'] = (
                f'scale({box_width}, {box_height})')
    elif not (size(surface, pattern_node.get('width', 1), 1) and
            size(surface, pattern_node.get('height', 1), 1)):
        return False

    # Opacity is a property of this reference, not of the definition.
    pattern_node['opacity'] = (
        float(pattern_node.get('opacity', 1)) * opacity)

    from .surface import SVGSurface  # circular import
    pattern_surface = SVGSurface(pattern_node, None, surface.dpi, surface)
    pattern_pattern = cairo.SurfacePattern(pattern_surface.cairo)
    pattern_pattern.set_extend(cairo.EXTEND_REPEAT)

    # Cairo pattern matrices map user space onto pattern space, exactly like
    # the SVG pattern matrix: translation by the tile origin followed by the
    # scale between pattern-surface pixels and user-space tile units.
    pattern_matrix = cairo.Matrix(
        pattern_surface.width / pattern_width, 0, 0,
        pattern_surface.height / pattern_height, -x, -y)

    # patternTransform applies to the whole tiling system (tiles, origin and
    # phase), never to the painted geometry itself, so it goes into the
    # pattern matrix rather than the context's current transformation matrix.
    # The inverted transform is composed after the base matrix (the same
    # ordering used for gradientTransform) so the tile origin (x, y) is
    # transformed together with the tiles, as required by the SVG spec.
    pattern_transform = parse_transform(
        surface, pattern_node.get('patternTransform'))
    if pattern_transform is not None:
        try:
            pattern_transform.invert()
        except cairo.Error:
            # A non-invertible transform disables the paint server.
            return False
        pattern_matrix = pattern_transform.multiply(pattern_matrix)
    pattern_pattern.set_matrix(pattern_matrix)
    surface.context.set_source(pattern_pattern)
    return True


def prepare_filter(surface, node, name):
    """Apply a filter transforming the context."""
    if 'id' in node and node['id'] in surface.masks:
        return

    if name in surface.filters:
        filter_node = surface.filters[name]
        for child in filter_node.children:
            # Offset
            if child.tag == 'feOffset':
                if filter_node.get('primitiveUnits') == 'objectBoundingBox':
                    width = size(surface, node.get('width'), 'x')
                    height = size(surface, node.get('height'), 'y')
                    dx = size(surface, child.get('dx', 0), 1) * width
                    dy = size(surface, child.get('dy', 0), 1) * height
                else:
                    dx = size(surface, child.get('dx', 0), 1)
                    dy = size(surface, child.get('dy', 0), 1)
                surface.context.translate(dx, dy)


def apply_filter_before_painting(surface, node, name):
    """Apply a filter transforming the painting operations."""
    if 'id' in node and node['id'] in surface.masks:
        return

    if name in surface.filters:
        filter_node = surface.filters[name]
        for child in filter_node.children:
            # Blend
            if child.tag == 'feBlend':
                surface.context.set_operator(BLEND_OPERATORS.get(
                    child.get('mode', 'normal'), BLEND_OPERATORS['normal']))


def apply_filter_after_painting(surface, node, name):
    """Apply a filter using the painted surface to transform the image."""
    if 'id' in node and node['id'] in surface.masks:
        return

    if name in surface.filters:
        filter_node = surface.filters[name]
        for child in filter_node.children:
            # Flood
            if child.tag == 'feFlood':
                surface.context.save()
                surface.context.new_path()
                if filter_node.get('primitiveUnits') == 'objectBoundingBox':
                    x = size(surface, node.get('x'), 'x')
                    y = size(surface, node.get('y'), 'y')
                    width = size(surface, node.get('width'), 'x')
                    height = size(surface, node.get('height'), 'y')
                else:
                    x, y, width, height = 0, 0, 1, 1
                x += size(surface, child.get('x', 0), 1)
                y += size(surface, child.get('y', 0), 1)
                width *= size(surface, child.get('width', 0), 1)
                height *= size(surface, child.get('height', 0), 1)
                rect(surface, dict(x=x, y=y, width=width, height=height))
                surface.context.set_source_rgba(*surface.map_color(
                    paint(child.get('flood-color'))[1],
                    float(child.get('flood-opacity', 1))))
                surface.context.fill()
                surface.context.restore()


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
