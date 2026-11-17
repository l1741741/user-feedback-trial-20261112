"""
Cairo surface creators.

"""

import copy
import io
import math

import cairocffi as cairo

from .bounding_box import (
    calculate_bounding_box, is_non_empty_bounding_box)
from .colors import color, negate_color
from .defs import (
    apply_filter_after_painting, apply_filter_before_painting, clip_path,
    filter_, gradient_or_pattern, linear_gradient, marker, mask, paint_mask,
    parse_all_defs, pattern, prepare_filter, radial_gradient, use)
from .helpers import (
    UNITS, PointError, clip_rect, node_format, normalize, paint,
    preserve_ratio, size, transform)
from .image import image, invert_image
from .parser import Tree
from .path import draw_markers, path
from .shapes import circle, ellipse, line, polygon, polyline, rect
from .svg import svg
from .text import text
from .url import parse_url

SHAPE_ANTIALIAS = {
    'optimizeSpeed': cairo.ANTIALIAS_FAST,
    'crispEdges': cairo.ANTIALIAS_NONE,
    'geometricPrecision': cairo.ANTIALIAS_BEST,
}

TEXT_ANTIALIAS = {
    'crispEdges': cairo.ANTIALIAS_NONE,
    'optimizeSpeed': cairo.ANTIALIAS_FAST,
    'optimizeLegibility': cairo.ANTIALIAS_GOOD,
    'geometricPrecision': cairo.ANTIALIAS_BEST,
}

TEXT_HINT_STYLE = {
    'geometricPrecision': cairo.HINT_STYLE_NONE,
    'optimizeLegibility': cairo.HINT_STYLE_FULL,
}

TEXT_HINT_METRICS = {
    'geometricPrecision': cairo.HINT_METRICS_OFF,
    'optimizeLegibility': cairo.HINT_METRICS_ON,
}

TAGS = {
    'a': text,
    'circle': circle,
    'clipPath': clip_path,
    'ellipse': ellipse,
    'filter': filter_,
    'image': image,
    'line': line,
    'linearGradient': linear_gradient,
    'marker': marker,
    'mask': mask,
    'path': path,
    'pattern': pattern,
    'polyline': polyline,
    'polygon': polygon,
    'radialGradient': radial_gradient,
    'rect': rect,
    'svg': svg,
    'text': text,
    'textPath': text,
    'tspan': text,
    'use': use,
}

PATH_TAGS = frozenset((
    'circle', 'ellipse', 'line', 'path', 'polygon', 'polyline', 'rect'))

INVISIBLE_TAGS = frozenset((
    'clipPath', 'filter', 'linearGradient', 'marker', 'mask', 'pattern',
    'radialGradient', 'symbol'))

# SVG keyword values for stroke-linejoin / stroke-linecap. SVG 1.1 only knows
# miter, round and bevel (plus butt, round and square for caps); newer
# specifications add miter-clip and arcs, which Cairo has no equivalent for and
# that are drawn as miter here. Values are matched case-insensitively, like any
# other paint keyword.
LINE_JOINS = {
    'miter': cairo.LINE_JOIN_MITER,
    'miter-clip': cairo.LINE_JOIN_MITER,
    'arcs': cairo.LINE_JOIN_MITER,
    'round': cairo.LINE_JOIN_ROUND,
    'bevel': cairo.LINE_JOIN_BEVEL,
}
LINE_CAPS = {
    'butt': cairo.LINE_CAP_BUTT,
    'round': cairo.LINE_CAP_ROUND,
    'square': cairo.LINE_CAP_SQUARE,
}

# ``stroke-miterlimit`` defaults to 4 and is only accepted as a number. Values
# that cannot be used (garbage, units, NaN, infinities, huge numbers that make
# Cairo drop the stroke) are ignored, so the inherited value keeps applying.
# Negative and sub-1 numbers are still forwarded to Cairo: that is the
# behaviour existing documents rely on.
DEFAULT_MITER_LIMIT = 4
MAX_MITER_LIMIT = 1e10

# Cairo silently draws nothing when a subpath is made of exactly one point, so
# the end caps of that subpath disappear. SVG requires zero-length subpaths
# with ``round`` or ``square`` caps to be painted as a dot or a square. A
# subpath point is therefore moved by a tiny amount along the x axis, in device
# space, to give Cairo a direction to stroke. The offset is far below one
# device pixel (hence invisible) but large enough for Cairo to keep the
# segment.
CAP_EPSILON = 1e-2


def parse_number(string, minimum=None, maximum=None):
    """Parse an SVG number, returning ``None`` when it is invalid.

    ``minimum`` and ``maximum`` also reject NaN and infinities, which Cairo
    accepts but which can make the whole stroke disappear.

    """
    try:
        value = float(string)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return maximum
    return value


def parse_keyword(string):
    """Normalize an SVG keyword: surrounding spaces removed, lowercased."""
    if string is None:
        return None
    return ' '.join(str(string).split()).lower()


def parse_line_join(string):
    """Parse a ``stroke-linejoin`` value, returning ``None`` if invalid."""
    return LINE_JOINS.get(parse_keyword(string))


def parse_line_cap(string):
    """Parse a ``stroke-linecap`` value, returning ``None`` if invalid."""
    return LINE_CAPS.get(parse_keyword(string))


def parse_miter_limit(string):
    """Parse a ``stroke-miterlimit`` value, returning ``None`` if invalid.

    A numeric value keeps the behaviour it always had: finite values are given
    to Cairo as is, including the ones below 1, so existing documents keep
    rendering the same. Only values that cannot be used are rejected: garbage,
    units, NaN, infinities (Cairo draws *nothing* when the limit is infinite)
    and negative numbers (Cairo then paints a round join). Huge finite values
    are bounded, because Cairo silently drops the stroke around ``1e14``.

    """
    value = parse_number(string)
    if value is None or value < 0:
        return None
    return min(value, MAX_MITER_LIMIT)


def inherited_value(node, attribute, parse, initial):
    """Resolve an inherited paint property for ``node``.

    The parser already copies inherited attributes onto every node, so the
    attribute of ``node`` normally holds the value that applies. When it holds
    an invalid value, SVG ignores that declaration and the value inherited from
    the ancestors keeps applying, which ``node.parent`` provides. The walk
    stops at the first valid value, so the initial value of the property is
    only used when nothing valid can be found up to the root.

    """
    current = node
    while current is not None:
        value = parse(current.get(attribute))
        if value is not None:
            return value
        current = getattr(current, 'parent', None)
    return initial


def line_join(node):
    """Return the Cairo line join applying to ``node``."""
    return inherited_value(
        node, 'stroke-linejoin', parse_line_join, cairo.LINE_JOIN_MITER)


def line_cap(node):
    """Return the Cairo line cap applying to ``node``."""
    return inherited_value(
        node, 'stroke-linecap', parse_line_cap, cairo.LINE_CAP_BUTT)


def miter_limit(node):
    """Return the ``stroke-miterlimit`` applying to ``node``."""
    return inherited_value(
        node, 'stroke-miterlimit', parse_miter_limit, DEFAULT_MITER_LIMIT)


def has_visible_stroke(surface, node):
    """Know whether ``node`` is painted with a visible stroke."""
    if not surface.stroke_and_fill or node.get('stroke') in (None, 'none'):
        return False
    width = size(surface, node.get('stroke-width', '1'))
    return bool(width and math.isfinite(width) and width > 0)


def split_subpaths(path):
    """Split a Cairo path (as returned by ``copy_path``) into subpaths.

    Each subpath is a ``(items, closed)`` tuple. When a path ends with
    ``CLOSE_PATH``, Cairo appends a ``MOVE_TO`` back to the subpath's start
    point; that repeated move belongs to the closed subpath and is absorbed
    here so that ``is_zero_length`` only sees the operations that were
    actually specified.

    """
    path = list(path)
    if (len(path) > 1 and path[-1][0] == cairo.PATH_MOVE_TO and
            path[-2][0] == cairo.PATH_CLOSE_PATH):
        path.pop()

    subpaths = []
    items = []
    for operation, arguments in path:
        if operation == cairo.PATH_MOVE_TO and items:
            subpaths.append((items, False))
            items = []
        items.append((operation, arguments))
        if operation == cairo.PATH_CLOSE_PATH:
            subpaths.append((items, True))
            items = []
    if items:
        subpaths.append((items, False))
    return subpaths


def is_zero_length(subpath):
    """Know whether a subpath only draws a single point.

    Such a subpath is a ``MOVE_TO`` followed by nothing but ``LINE_TO`` to the
    same coordinates and/or ``CLOSE_PATH``. A lone ``MOVE_TO`` is not a
    zero-length subpath: SVG only asks for caps once the path actually starts
    drawing, and Cairo ignores it too.

    """
    items, _ = subpath
    if len(items) < 2 or items[0][0] != cairo.PATH_MOVE_TO:
        return False
    start_x, start_y = items[0][1]
    for operation, arguments in items[1:]:
        if operation == cairo.PATH_CLOSE_PATH:
            continue
        if operation != cairo.PATH_LINE_TO:
            return False
        if arguments[0] != start_x or arguments[1] != start_y:
            return False
    return True


def draw_zero_length_caps(surface, node):
    """Draw caps for the zero-length subpaths of the current context path.

    Cairo only strokes subpaths that have a direction: ``M20,20 L20,20`` is
    dropped, while SVG asks for a round dot or a square. Each single-point
    subpath is removed from the current path and stroked separately as a tiny
    horizontal segment whose caps paint exactly the required shape. The offset
    is far below a device pixel, so it is invisible and does not change the
    painted area; it only gives Cairo the direction it needs.

    The stroke style (colour, source, width, dashes, transform) is the one set
    up for the element, so the dots and squares match the rest of the stroke,
    including the dash phase. Paths are left untouched when the stroke is not
    painted (no stroke, zero width, clip paths, masks, markers), so fill-only
    usages keep an exact geometry.

    """
    if not has_visible_stroke(surface, node):
        return
    if line_cap(node) == cairo.LINE_CAP_BUTT:
        # Cairo already paints no cap for a single-point subpath.
        return

    subpaths = split_subpaths(surface.context.copy_path())
    degenerate = [
        subpath for subpath in subpaths if is_zero_length(subpath)]
    if not degenerate:
        return

    # Remove the single-point subpaths from the current path: they are drawn
    # below, and Cairo would ignore them anyway.
    kept = [
        item for subpath in subpaths if not is_zero_length(subpath)
        for item in subpath[0]]
    surface.context.new_path()
    surface.context.append_path(kept)

    # A hundredth of a device pixel is invisible but long enough for Cairo to
    # keep the segment at any scale. The CTM x scale converts it to user space.
    matrix = surface.context.get_matrix()
    scale_x = math.hypot(matrix.xx, matrix.yx)
    distance = CAP_EPSILON / scale_x if scale_x else CAP_EPSILON

    # Draw the caps now, then restore the element path so that the caller
    # strokes exactly the geometry it built (the path is not part of the saved
    # graphics state, so it has to be saved and put back explicitly).
    surface.context.save()
    try:
        for subpath in degenerate:
            x, y = subpath[0][0][1]
            surface.context.move_to(x, y)
            surface.context.line_to(x + distance, y)
        surface.context.stroke()
    finally:
        surface.context.restore()
        surface.context.new_path()
        surface.context.append_path(kept)


class Surface(object):
    """Abstract base class for CairoSVG surfaces.

    The ``width`` and ``height`` attributes are in device units (pixels for
    PNG, else points).

    The ``context_width`` and ``context_height`` attributes are in user units
    (i.e. in pixels), they represent the size of the active viewport.

    """

    # Subclasses must either define this or override _create_surface()
    surface_class = None

    @classmethod
    def convert(cls, bytestring=None, *, file_obj=None, url=None, dpi=96,
                parent_width=None, parent_height=None, scale=1, unsafe=False,
                background_color=None, negate_colors=False,
                invert_images=False, write_to=None, output_width=None,
                output_height=None, **kwargs):
        """Convert an SVG document to the format for this class.

        Specify the input by passing one of these:

        :param bytestring: The SVG source as a byte-string.
        :param file_obj: A file-like object.
        :param url: A filename, given as a string or a path-like object.

        Give some options:

        :param dpi: The ratio between 1 inch and 1 pixel.
        :param parent_width: The width of the parent container in pixels.
        :param parent_height: The height of the parent container in pixels.
        :param scale: The ouptut scaling factor.
        :param unsafe: A boolean allowing external file access, XML entities
                       and very large files
                       (WARNING: vulnerable to XXE attacks and various DoS).
                       This does NOT restrict fetching of the main SVG input
                       specified via the ``url`` parameter.
                       Applications should validate input URLs to prevent SSRF.


        Specifiy the output with:

        :param write_to: The filename of file-like object where to write the
                         output. If None or not provided, return a byte string.

        Only ``bytestring`` can be passed as a positional argument, other
        parameters are keyword-only.

        """
        tree = Tree(
            bytestring=bytestring, file_obj=file_obj, url=url, unsafe=unsafe,
            **kwargs)
        output = write_to or io.BytesIO()
        instance = cls(
            tree, output, dpi, None, parent_width, parent_height, scale,
            output_width, output_height, background_color,
            map_rgba=negate_color if negate_colors else None,
            map_image=invert_image if invert_images else None)
        instance.finish()
        if write_to is None:
            return output.getvalue()

    def __init__(self, tree, output, dpi, parent_surface=None,
                 parent_width=None, parent_height=None,
                 scale=1, output_width=None, output_height=None,
                 background_color=None, map_rgba=None, map_image=None):
        """Create the surface from a filename or a file-like object.

        The rendered content is written to ``output`` which can be a filename,
        a file-like object, ``None`` (render in memory but do not write
        anything) or the built-in ``bytes`` as a marker.

        Call the ``.finish()`` method to make sure that the output is
        actually written.

        """
        self.cairo = None
        self.context_width, self.context_height = parent_width, parent_height
        self.cursor_position = [0, 0]
        self.cursor_d_position = [0, 0]
        self.text_path_width = 0
        self.tree_cache = {(tree.url, tree.get('id')): tree}
        self.reference_count = 0
        if parent_surface:
            self.markers = parent_surface.markers
            self.gradients = parent_surface.gradients
            self.patterns = parent_surface.patterns
            self.masks = parent_surface.masks
            self.paths = parent_surface.paths
            self.filters = parent_surface.filters
            self.images = parent_surface.images
        else:
            self.markers = {}
            self.gradients = {}
            self.patterns = {}
            self.masks = {}
            self.paths = {}
            self.filters = {}
            self.images = {}
        self._old_parent_node = self.parent_node = None
        self.output = output
        self.dpi = dpi
        self.font_size = size(self, '12pt')
        self.stroke_and_fill = True
        width, height, viewbox = node_format(self, tree)
        if viewbox is None:
            viewbox = (0, 0, width, height)

        if output_width and output_height:
            width, height = output_width, output_height
        elif output_width:
            if width:
                # Keep the aspect ratio
                height *= output_width / width
            width = output_width
        elif output_height:
            if height:
                # Keep the aspect ratio
                width *= output_height / height
            height = output_height
        else:
            width *= scale
            height *= scale

        # Actual surface dimensions: may be rounded on raster surfaces types
        self.cairo, self.width, self.height = self._create_surface(
            width * self.device_units_per_user_units,
            height * self.device_units_per_user_units)

        if 0 in (self.width, self.height):
            raise ValueError('The SVG size is undefined')

        self.context = cairo.Context(self.cairo)
        # We must scale the context as the surface size is using physical units
        self.context.scale(
            self.device_units_per_user_units, self.device_units_per_user_units)
        # Initial, non-rounded dimensions
        self.set_context_size(width, height, viewbox, tree)
        self.context.move_to(0, 0)

        if background_color:
            self.context.set_source_rgba(*color(background_color))
            self.context.paint()

        self.map_rgba = map_rgba
        self.map_image = map_image
        self.draw(tree)

    @property
    def points_per_pixel(self):
        """Surface resolution."""
        return 1 / (self.dpi * UNITS['pt'])

    @property
    def device_units_per_user_units(self):
        """Ratio between Cairo device units and user units.

        Device units are pixels for PNG and SVG, and points for everything
        else. User units are pixels.

        """
        return self.points_per_pixel

    def _create_surface(self, width, height):
        """Create and return ``(cairo_surface, width, height)``."""
        cairo_surface = self.surface_class(self.output, width, height)
        return cairo_surface, width, height

    def set_context_size(self, width, height, viewbox, tree):
        """Set the Cairo context size, set the SVG viewport size."""
        if viewbox:
            rect_x, rect_y = viewbox[0:2]
            tree.image_width = viewbox[2]
            tree.image_height = viewbox[3]
        else:
            rect_x, rect_y = 0, 0
            tree.image_width = width
            tree.image_height = height

        scale_x, scale_y, translate_x, translate_y = preserve_ratio(
            self, tree, width, height)
        rect_x, rect_y = rect_x * scale_x, rect_y * scale_y
        rect_width, rect_height = width, height
        self.context.translate(*self.context.get_current_point())
        self.context.translate(-rect_x, -rect_y)
        if tree.get('overflow', 'hidden') != 'visible':
            self.context.rectangle(rect_x, rect_y, rect_width, rect_height)
            self.context.clip()
        self.context.scale(scale_x, scale_y)
        self.context.translate(translate_x, translate_y)
        self.context_width = rect_width / scale_x
        self.context_height = rect_height / scale_y

    def finish(self):
        """Read the surface content."""
        self.cairo.finish()

    def map_color(self, string, opacity=1):
        """Parse a color ``string`` and apply ``map_rgba`` function to it."""
        rgba = color(string, opacity)
        return self.map_rgba(rgba) if self.map_rgba else rgba

    def draw(self, node):
        """Draw ``node`` and its children."""

        # Parse definitions first
        if node.tag == 'svg':
            parse_all_defs(self, node)

        # Do not draw defs
        if node.tag == 'defs':
            return

        # Do not draw elements with width or height of 0
        if (('width' in node and size(self, node['width']) == 0) or
                ('height' in node and size(self, node['height']) == 0)):
            return

        # Save context and related attributes
        old_parent_node = self.parent_node
        old_font_size = self.font_size
        old_context_size = self.context_width, self.context_height
        self.parent_node = node

        if "font" in node:
            font = parse_font(node["font"])
            for att in font:
                if att not in node:
                    node[att] = font[att]

        self.font_size = size(self, node.get('font-size', '12pt'))
        self.context.save()

        # Apply transformations
        transform(
            self, node.get('transform'),
            transform_origin=node.get('transform-origin'))

        # Find and prepare opacity, masks and filters
        mask = parse_url(node.get('mask')).fragment
        filter_ = parse_url(node.get('filter')).fragment
        opacity = float(node.get('opacity', 1))

        if filter_:
            prepare_filter(self, node, filter_)

        if filter_ or mask or (opacity < 1 and node.children):
            self.context.push_group()

        # Move to (node.x, node.y)
        self.context.move_to(
            size(self, node.get('x'), 'x'),
            size(self, node.get('y'), 'y'))

        # Set the stroke style. Explicit SVG keywords always win over the
        # inherited state, so that a child asking for « miter » or « butt »
        # inside a « round » group gets it; unknown values fall back to the
        # default of the property rather than to the parent's state.
        self.context.set_line_cap(line_cap(node))
        self.context.set_line_join(line_join(node))
        self.context.set_miter_limit(miter_limit(node))

        dash_array = normalize(node.get('stroke-dasharray', '')).split()
        if dash_array:
            dashes = [size(self, dash) for dash in dash_array]
            # Cairo refuses a dash list made of zeros or containing negative
            # values, and the whole context then enters
            # CAIRO_STATUS_INVALID_DASH (every later drawing call raises).
            # SVG treats such a list as invalid, which just means "no dash":
            # keep the plain stroke and leave the context alone.
            if (sum(dashes) and all(
                    math.isfinite(dash) and dash >= 0 for dash in dashes)):
                offset = size(self, node.get('stroke-dashoffset'))
                self.context.set_dash(dashes, offset)

        # Clip
        rect_values = clip_rect(node.get('clip'))
        if len(rect_values) == 4:
            top = size(self, rect_values[0], 'y')
            right = size(self, rect_values[1], 'x')
            bottom = size(self, rect_values[2], 'y')
            left = size(self, rect_values[3], 'x')
            x = size(self, node.get('x'), 'x')
            y = size(self, node.get('y'), 'y')
            width = size(self, node.get('width'), 'x')
            height = size(self, node.get('height'), 'y')
            self.context.save()
            self.context.translate(x, y)
            self.context.rectangle(
                left, top, width - left - right, height - top - bottom)
            self.context.restore()
            self.context.clip()
        clip_path = parse_url(node.get('clip-path')).fragment
        if clip_path:
            path = self.paths.get(clip_path)
            if path:
                empty_box = False
                self.context.save()
                if path.get('clipPathUnits') == 'objectBoundingBox':
                    # The box is the node's own geometry box, not its x/y/width
                    # attributes: a path, circle or polygon has no such
                    # attributes and used to scale the clip path by zero, which
                    # makes the matrix singular and poisons the context.
                    box = calculate_bounding_box(self, node)
                    if is_non_empty_bounding_box(box):
                        x, y, width, height = box
                        self.context.translate(x, y)
                        self.context.scale(width, height)
                    else:
                        # A box without area clips everything out. Drawing the
                        # clip path here would use the unscaled coordinates.
                        empty_box = True
                if empty_box:
                    self.context.new_path()
                else:
                    path.tag = 'g'
                    self.stroke_and_fill = False
                    self.draw(path)
                    self.stroke_and_fill = True
                    path.tag = 'clipPath'
                self.context.restore()
                # TODO: fill rules are not handled by cairo for clips
                # if node.get('clip-rule') == 'evenodd':
                #     self.context.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
                self.context.clip()
                self.context.set_fill_rule(cairo.FILL_RULE_WINDING)

        save_cursor = copy.deepcopy(
            (self.cursor_position, self.cursor_d_position,
             self.text_path_width))

        # Only draw known tags
        if node.tag in TAGS:
            try:
                TAGS[node.tag](self, node)
            except PointError:
                # Error in point parsing, do nothing
                pass

        # Get stroke and fill opacity
        stroke_opacity = float(node.get('stroke-opacity', 1))
        fill_opacity = float(node.get('fill-opacity', 1))
        if opacity < 1 and not node.children:
            stroke_opacity *= opacity
            fill_opacity *= opacity

        # Manage display and visibility
        display = node.get('display', 'inline') != 'none'
        visible = display and (node.get('visibility', 'visible') != 'hidden')

        # Set font rendering properties
        self.context.set_antialias(SHAPE_ANTIALIAS.get(
            node.get('shape-rendering'), cairo.ANTIALIAS_DEFAULT))

        font_options = self.context.get_font_options()
        font_options.set_antialias(TEXT_ANTIALIAS.get(
            node.get('text-rendering'), cairo.ANTIALIAS_DEFAULT))
        font_options.set_hint_style(TEXT_HINT_STYLE.get(
            node.get('text-rendering'), cairo.HINT_STYLE_DEFAULT))
        font_options.set_hint_metrics(TEXT_HINT_METRICS.get(
            node.get('text-rendering'), cairo.HINT_METRICS_DEFAULT))
        self.context.set_font_options(font_options)

        # Fill and stroke
        if self.stroke_and_fill and visible and node.tag in TAGS:
            # Fill
            self.context.save()
            paint_source, paint_color = paint(node.get('fill', 'black'))
            if node.get('fill-rule') == 'evenodd':
                self.context.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
            if not gradient_or_pattern(self, node, paint_source, fill_opacity):
                self.context.set_source_rgba(
                    *self.map_color(paint_color, fill_opacity))
            if TAGS[node.tag] == text:
                self.cursor_position = save_cursor[0]
                self.cursor_d_position = save_cursor[1]
                self.text_path_width = save_cursor[2]
                text(self, node, draw_as_text=True)
            else:
                self.context.fill_preserve()
            self.context.restore()

            # Stroke
            self.context.save()
            self.context.set_line_width(
                size(self, node.get('stroke-width', '1')))
            paint_source, paint_color = paint(node.get('stroke'))
            if not gradient_or_pattern(
                    self, node, paint_source, stroke_opacity):
                self.context.set_source_rgba(
                    *self.map_color(paint_color, stroke_opacity))
            # Cairo drops single-point subpaths; SVG paints their caps.
            draw_zero_length_caps(self, node)
            self.context.stroke()
            self.context.restore()
        elif not visible:
            self.context.new_path()

        # Draw path markers
        draw_markers(self, node)

        # Draw children
        if display and node.tag not in INVISIBLE_TAGS:
            for child in node.children:
                self.draw(child)

        # Apply filter, mask and opacity
        if filter_ or mask or (opacity < 1 and node.children):
            self.context.pop_group_to_source()
            if filter_:
                apply_filter_before_painting(self, node, filter_)
            if mask in self.masks:
                paint_mask(self, node, mask, opacity)
            else:
                self.context.paint_with_alpha(opacity)
            if filter_:
                apply_filter_after_painting(self, node, filter_)

        # Clean cursor's position after 'text' tags
        if node.tag == 'text':
            self.cursor_position = [0, 0]
            self.cursor_d_position = [0, 0]
            self.text_path_width = 0

        self.context.restore()
        self.parent_node = old_parent_node
        self.font_size = old_font_size
        self.context_width, self.context_height = old_context_size


class PDFSurface(Surface):
    """A surface that writes in PDF format."""
    surface_class = cairo.PDFSurface


class PSSurface(Surface):
    """A surface that writes in PostScript format."""
    surface_class = cairo.PSSurface


class EPSSurface(Surface):
    """A surface that writes in Encapsulated PostScript format."""

    def _create_surface(self, width, height):
        """Create and return ``(cairo_surface, width, height)``."""
        cairo_surface = cairo.PSSurface(self.output, width, height)
        cairo_surface.set_eps(True)
        return cairo_surface, width, height


class PNGSurface(Surface):
    """A surface that writes in PNG format."""
    device_units_per_user_units = 1

    def _create_surface(self, width, height):
        """Create and return ``(cairo_surface, width, height)``."""
        width = int(round(width))
        height = int(round(height))
        cairo_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        return cairo_surface, width, height

    def finish(self):
        """Read the PNG surface content."""
        if self.output is not None:
            self.cairo.write_to_png(self.output)
        return super().finish()


class SVGSurface(Surface):
    """A surface that writes in SVG format.

    It may seem pointless to render SVG to SVG, but this can be used
    with ``output=None`` to get a vector-based single page cairo surface.

    """
    device_units_per_user_units = 1
    surface_class = cairo.SVGSurface


def parse_font(value):
    ret = {"font-family": "", "font-size": "", "font-style": "normal",
           "font-variant": "normal", "font-weight": "normal",
           "line-height": "normal"}

    font_styles = ["italic", "oblique"]
    font_variants = ["small-caps"]
    font_weights = ["bold", "bolder", "lighter", "100", "200", "300", "400",
                    "500", "600", "700", "800", "900"]

    for element in value.split():
        if element == "normal":
            continue
        elif ret["font-family"]:
            ret["font-family"] += " " + element
        elif element in font_styles:
            ret["font-style"] = element
        elif element in font_variants:
            ret["font-variant"] = element
        elif element in font_weights:
            ret["font-weight"] = element
        else:
            if not ret["font-size"]:
                parts = element.split("/")
                ret["font-size"] = parts[0]
                if len(parts) > 1:
                    ret["line-height"] = parts[1]
                continue
            else:
                ret["font-family"] = element

    return ret
