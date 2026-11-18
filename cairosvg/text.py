"""
Text drawers.

"""

from math import hypot, radians

from .bounding_box import (
    EMPTY_BOUNDING_BOX, extend_bounding_box, is_valid_bounding_box)
from .helpers import normalize, point_angle, size, zip_letters
from .surface import cairo
from .url import parse_url


def path_length(path):
    """Get the length of ``path``.

    ``path`` is a cairo flattened path (as returned by
    ``Context.copy_path_flat``) split into subpaths by
    :func:`subpath_segments`. Move-to items separate subpaths and contribute
    no length, close-path items are replaced by the closing segment back to
    the subpath start.

    """
    total_length = 0
    for points in subpath_segments(path):
        for index in range(1, len(points)):
            old_point = points[index - 1]
            new_point = points[index]
            total_length += distance(
                old_point[0], old_point[1], new_point[0], new_point[1])
    return total_length


def distance(x1, y1, x2, y2):
    """Get the distance between two points."""
    return hypot(x2 - x1, y2 - y1)


def subpath_segments(path):
    """Yield each subpath of a flattened cairo path as a list of points.

    Curves are flattened to line segments by cairo itself. A close-path item
    closes the current subpath with an explicit segment back to its starting
    point, instead of being silently dropped. Zero-length segments (including
    empty subpaths reduced to a single point) are kept, so that they still
    define a deterministic position on the path.

    """
    subpaths = []
    points = []
    subpath_start = None
    for item in path:
        if item[0] == cairo.PATH_MOVE_TO:
            if points:
                subpaths.append(points)
            points = [item[1]]
            subpath_start = item[1]
        elif item[0] in (cairo.PATH_LINE_TO, cairo.PATH_CURVE_TO):
            if item[0] == cairo.PATH_CURVE_TO:
                values = item[1]
                points.extend(
                    values[index:index + 2]
                    for index in (0, 2, 4))
            else:
                points.append(item[1])
        elif item[0] == cairo.PATH_CLOSE_PATH and subpath_start is not None:
            if points[-1] != subpath_start:
                points.append(subpath_start)
    if points:
        # Cairo ends a closed subpath with a redundant move-to back to its
        # start point; a lone point repeating the previous subpath end is not
        # another subpath and would otherwise inflate the path structure.
        if (len(points) == 1 and subpaths
                and points[0] == subpaths[-1][-1]):
            pass
        else:
            subpaths.append(points)
    return subpaths


def point_following_path(path, width):
    """Get the point and tangent at ``width`` distance on ``path``.

    Return ``(x, y, angle)`` where ``angle`` is the direction of the path at
    that point, following the direction of the subpath containing it. Gaps
    between subpaths are not part of the path: once a subpath ends, positions
    further along the distance continue at the next subpath without jumping
    across the gap. Return ``None`` when ``width`` is outside of the path or
    when the path is empty.

    """
    total_length = 0
    if width < 0:
        return None
    for points in subpath_segments(path):
        subpath_start = total_length
        for index in range(1, len(points)):
            old_point = points[index - 1]
            new_point = points[index]
            segment_length = distance(
                old_point[0], old_point[1], new_point[0], new_point[1])
            total_length += segment_length
            if total_length >= width:
                angle = point_angle(
                    old_point[0], old_point[1], new_point[0], new_point[1])
                if segment_length:
                    remaining = width - (total_length - segment_length)
                    ratio = remaining / segment_length
                else:
                    ratio = 0
                x = old_point[0] + (new_point[0] - old_point[0]) * ratio
                y = old_point[1] + (new_point[1] - old_point[1]) * ratio
                return x, y, angle
        # Zero-length subpath: a single move-to point is still a position.
        if len(points) == 1 and width == subpath_start:
            point = points[0]
            return point[0], point[1], 0
    return None


def text(surface, node, draw_as_text=False):
    """Draw a text ``node``."""
    font_family = (
        (node.get('font-family') or 'sans-serif').split(',')[0].strip('"\' '))
    font_style = getattr(
        cairo, f'font_slant_{node.get("font-style")}'.upper(),
        cairo.FONT_SLANT_NORMAL)
    node_font_weight = node.get('font-weight')
    if (node_font_weight and node_font_weight.isdigit()
            and int(node_font_weight) >= 550):
        node_font_weight = 'bold'
    font_weight = getattr(
        cairo, (f'font_weight_{node_font_weight}'.upper()),
        cairo.FONT_WEIGHT_NORMAL)
    surface.context.select_font_face(font_family, font_style, font_weight)
    surface.context.set_font_size(surface.font_size)
    ascent, descent, _, max_x_advance, max_y_advance = (
        surface.context.font_extents())

    text_path_href = parse_url(node.get_href() or node.parent.get_href() or '')
    if text_path_href.fragment:
        text_path = surface.paths.get(text_path_href.fragment)
    else:
        text_path = None
    # A tspan nested inside a textPath continues the same path layout, even
    # though it does not carry the href itself. A textPath whose href cannot
    # be resolved still behaves as path text: its content is disabled rather
    # than falling back to ordinary text at the origin.
    on_text_path = (
        text_path is not None or _ancestor_is_text_path(node) or
        node.tag == 'textPath')
    letter_spacing = size(surface, node.get('letter-spacing'))
    x_bearing, y_bearing, width, height = (
        surface.context.text_extents(node.text)[:4])

    x, y, dx, dy, rotate = [], [], [], [], [0]
    if 'x' in node:
        x = [size(surface, i, 'x')
             for i in normalize(node['x']).strip().split(' ')]
    if 'y' in node:
        y = [size(surface, i, 'y')
             for i in normalize(node['y']).strip().split(' ')]
    if 'dx' in node:
        dx = [size(surface, i, 'x')
              for i in normalize(node['dx']).strip().split(' ')]
    if 'dy' in node:
        dy = [size(surface, i, 'y')
              for i in normalize(node['dy']).strip().split(' ')]
    if 'rotate' in node:
        rotate = [radians(float(i)) if i else 0
                  for i in normalize(node['rotate']).strip().split(' ')]
    last_r = rotate[-1]
    letters_positions = zip_letters(x, y, dx, dy, rotate, node.text)

    x_align = 0
    y_align = 0

    # text-anchor is inherited: a textPath nested in a <text> uses the anchor
    # defined on the nearest ancestor that specifies one.
    text_anchor = _inherited_attribute(node, 'text-anchor')
    if text_anchor == 'middle':
        x_align = - (width / 2 + x_bearing)
        if letter_spacing and node.text:
            x_align -= (len(node.text) - 1) * letter_spacing / 2
    elif text_anchor == 'end':
        x_align = - (width + x_bearing)
        if letter_spacing and node.text:
            x_align -= (len(node.text) - 1) * letter_spacing

    # On a path, glyph origins are positioned explicitly with absolute
    # coordinates: the anchor is applied by shifting the start position along
    # the path. Use the sum of the glyph advances (the actual length occupied
    # on the path) rather than the ink bounding-box width, which excludes side
    # bearings.
    path_text_width = sum(
        surface.context.text_extents(letter)[4]
        for letter in node.text) if node.text else 0
    path_x_align = 0
    if text_anchor == 'middle':
        path_x_align = -path_text_width / 2
        if letter_spacing and node.text:
            path_x_align -= (len(node.text) - 1) * letter_spacing / 2
    elif text_anchor == 'end':
        path_x_align = -path_text_width
        if letter_spacing and node.text:
            path_x_align -= (len(node.text) - 1) * letter_spacing

    # TODO: This is a hack. The rest of the baseline alignment tags of the SVG
    # 1.1 spec (section 10.9.2) are not supported. We only try to align things
    # that look like Western horizontal fonts.
    # Finally, we add a "display-anchor" attribute for aligning the specific
    # text rather than the font baseline.
    # Nonetheless, there are times when one needs to align text vertically, and
    # this will at least make that possible.
    if max_x_advance > 0 and max_y_advance == 0:
        display_anchor = node.get('display-anchor')
        alignment_baseline = (node.get('dominant-baseline') or
                              node.get('alignment-baseline'))
        if display_anchor == 'middle':
            y_align = -height / 2 - y_bearing
        elif display_anchor == 'top':
            y_align = -y_bearing
        elif display_anchor == 'bottom':
            y_align = -height - y_bearing
        elif (alignment_baseline == 'central' or
              alignment_baseline == 'middle'):
            # TODO: This is wrong, Cairo gives no reasonable access to x-height
            # information, so we use font top-to-bottom
            y_align = (ascent + descent) / 2 - descent
        elif (alignment_baseline == 'text-before-edge' or
              alignment_baseline == 'before_edge' or
              alignment_baseline == 'top' or
              alignment_baseline == 'hanging' or
              alignment_baseline == 'text-top'):
            y_align = ascent
        elif (alignment_baseline == 'text-after-edge' or
              alignment_baseline == 'after_edge' or
              alignment_baseline == 'bottom' or
              alignment_baseline == 'text-bottom'):
            y_align = -descent

    bounding_box = EMPTY_BOUNDING_BOX
    cairo_path = None
    path_total_length = 0
    if text_path is not None:
        cairo_path = build_text_path(surface, text_path)
        path_total_length = path_length(cairo_path)
        # Percentages in startOffset are relative to the length of the
        # referenced path, not to the viewport size (SVG 1.1 §10.13.1).
        start_offset = parse_start_offset(
            surface, node.get('startOffset', 0), path_total_length)
        if path_total_length:
            # Text-anchor shifts the whole text run along the path, in the
            # direction of the path. The anchor is applied exactly once, by
            # the textPath node carrying the href; outer text nodes and inner
            # tspans must not add their own (per-node width based) shift.
            if node.tag == 'textPath':
                surface.text_path_width += start_offset + path_x_align
            else:
                surface.text_path_width += start_offset
        bounding_box = extend_bounding_box(
            bounding_box, ((start_offset, 0),))
    elif on_text_path:
        # Nested tspan: reuse the flattened path built by the textPath root.
        cairo_path = surface.active_text_path
        path_total_length = surface.active_text_path_length

    # Text on a missing, empty or zero-length referenced path must not be
    # drawn at all, and it must not leave cursor state that would leak into
    # sibling text (SVG 1.1 §10.13.1: the textPath content is disabled).
    text_on_path = on_text_path and cairo_path is not None \
        and path_total_length > 0
    skip_text = on_text_path and not text_on_path
    if text_path is not None:
        surface.active_text_path = cairo_path
        surface.active_text_path_length = path_total_length

    if node.text and not skip_text:
        for i, ((x, y, dx, dy, r), letter) in enumerate(letters_positions):
            if x:
                surface.cursor_d_position[0] = 0
            if y:
                surface.cursor_d_position[1] = 0
            surface.cursor_d_position[0] += dx or 0
            surface.cursor_d_position[1] += dy or 0
            text_extents = surface.context.text_extents(letter)
            extents = text_extents[4]
            if text_on_path:
                # dx advances the current position along the path, while dy
                # shifts the glyph perpendicularly (here expressed in the
                # rotated glyph coordinate system). Both follow the path
                # direction, including on reversed subpaths.
                start = surface.text_path_width + surface.cursor_d_position[0]
                start_point = point_following_path(cairo_path, start)
                middle = start + extents / 2
                middle_point = point_following_path(cairo_path, middle)
                end = start + extents
                end_point = point_following_path(cairo_path, end)
                if i:
                    extents += letter_spacing
                surface.text_path_width += extents
                # Glyphs entirely outside of the path are skipped: a short
                # path never produces glyphs placed at the origin or beyond
                # its end.
                if not all((start_point, middle_point, end_point)):
                    continue
                if not 0 <= middle <= path_total_length:
                    continue
                surface.context.save()
                surface.context.translate(start_point[0], start_point[1])
                surface.context.rotate(start_point[2])
                surface.context.translate(0, surface.cursor_d_position[1])
                surface.context.move_to(0, 0)
                bounding_box = extend_bounding_box(
                    bounding_box, ((end_point[0], text_extents[3]),))
            else:
                surface.context.save()
                x = surface.cursor_position[0] if x is None else x
                y = surface.cursor_position[1] if y is None else y
                if i:
                    x += letter_spacing
                surface.context.move_to(x, y)
                cursor_position = x + extents, y
                surface.context.rel_move_to(*surface.cursor_d_position)
                surface.context.rel_move_to(x_align, y_align)
                surface.context.rotate(last_r if r is None else r)
                points = (
                    (cursor_position[0] + x_align +
                     surface.cursor_d_position[0],
                     cursor_position[1] + y_align +
                     surface.cursor_d_position[1]),
                    (cursor_position[0] + x_align + text_extents[4] +
                     surface.cursor_d_position[0],
                     cursor_position[1] + y_align + text_extents[3] +
                     surface.cursor_d_position[1]))
                bounding_box = extend_bounding_box(bounding_box, points)

            # Only draw characters with 'content' (workaround for bug in cairo)
            if not letter.isspace():
                if draw_as_text:
                    surface.context.show_text(letter)
                else:
                    surface.context.text_path(letter)
            surface.context.restore()
            if not text_on_path:
                surface.cursor_position = cursor_position
    elif not skip_text:
        x = x[0] if x else surface.cursor_position[0]
        y = y[0] if y else surface.cursor_position[1]
        dx = dx[0] if dx else 0
        dy = dy[0] if dy else 0
        surface.cursor_position = (x + dx, y + dy)

    if text_path is not None:
        surface.active_text_path = None
        surface.active_text_path_length = 0

    # If a valid bounding box is calculated store it in the node
    if is_valid_bounding_box(bounding_box):
        node['text_bounding_box'] = bounding_box


def build_text_path(surface, text_path):
    """Build the flattened cairo path referenced by a ``textPath`` node.

    The referenced node is drawn in a throw-away state so that the path
    geometry stored in ``surface.paths`` is never mutated, even when the same
    path is referenced several times or also drawn as a visible shape. Any
    shape usable as a path (``path``, ``polyline``, ``polygon``, ``line``,
    ``rect``, circles and ellipses) is supported, with the transforms
    currently applied to the context taken into account. Return an empty list
    for missing or empty paths.

    """
    surface.context.save()
    surface.context.new_path()
    old_stroke_and_fill = surface.stroke_and_fill
    surface.stroke_and_fill = False
    try:
        surface.draw(text_path)
        cairo_path = surface.context.copy_path_flat()
    finally:
        surface.stroke_and_fill = old_stroke_and_fill
        surface.context.new_path()
        surface.context.restore()
    return cairo_path


def parse_start_offset(surface, string, reference_length):
    """Parse a ``startOffset`` value.

    Numeric values are user units along the path. Percentages are relative to
    the path length, per the SVG 1.1 specification. Negative values and
    values beyond the path end are clamped to the path bounds.

    """
    if not string:
        return 0
    string = str(string).strip()
    if string.endswith('%'):
        try:
            value = float(string[:-1]) * reference_length / 100
        except ValueError:
            return 0
    else:
        value = size(surface, string)
    if reference_length:
        value = min(max(value, 0), reference_length)
    return value


def _ancestor_is_text_path(node):
    """Return whether ``node`` is nested inside a ``textPath`` element."""
    parent = getattr(node, 'parent', None)
    while parent is not None:
        if getattr(parent, 'tag', None) == 'textPath':
            return True
        parent = getattr(parent, 'parent', None)
    return False


def _inherited_attribute(node, name):
    """Return an attribute value, walking up the tree as inheritance does."""
    current = node
    while current is not None:
        value = current.get(name)
        if value is not None:
            return value
        current = getattr(current, 'parent', None)
    return None
