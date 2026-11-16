"""
Text drawers.

"""

from math import cos, hypot, radians, sin

from .bounding_box import (
    EMPTY_BOUNDING_BOX, extend_bounding_box, is_valid_bounding_box)
from .helpers import normalize, point_angle, size, zip_letters
from .surface import cairo
from .url import parse_url


def path_segments(path):
    """Split a flat cairo path into non-degenerate line segments.

    Curves are already flattened by cairo (``copy_path_flat``), so every
    drawing operation is a ``LINE_TO``. Closed subpaths (``CLOSE_PATH``) are
    completed with an explicit segment back to the subpath start point.
    Zero-length segments are ignored: they carry no direction and must not
    split a run of text.

    Return an iterable of ``(x1, y1, x2, y2)`` tuples.

    """
    subpath_start = None
    current_point = None
    for item in path:
        if item[0] == cairo.PATH_MOVE_TO:
            subpath_start = item[1]
            current_point = item[1]
        elif item[0] == cairo.PATH_LINE_TO:
            new_point = item[1]
            if current_point != new_point:
                yield (*current_point, *new_point)
            current_point = new_point
        elif item[0] == cairo.PATH_CLOSE_PATH:
            if (current_point is not None and subpath_start is not None
                    and current_point != subpath_start):
                yield (*current_point, *subpath_start)
            current_point = subpath_start


def path_length(path):
    """Get the geometric length of ``path``."""
    return sum(hypot(x2 - x1, y2 - y1)
               for x1, y1, x2, y2 in path_segments(path))


def point_following_path(path, width):
    """Get the point and tangent angle at ``width`` distance on ``path``.

    Distances before the start or past the end of the path are clamped to
    the first and last points, so callers always get a defined position.
    Return ``(x, y, angle)`` or ``None`` when the path has no drawable
    segment (i.e. its length is zero).

    """
    total_length = 0
    first_segment = None
    last_segment = None
    for x1, y1, x2, y2 in path_segments(path):
        if first_segment is None:
            first_segment = (x1, y1, x2, y2)
        last_segment = (x1, y1, x2, y2)
        segment_length = hypot(x2 - x1, y2 - y1)
        if total_length + segment_length >= width:
            remaining = width - total_length
            angle = point_angle(x1, y1, x2, y2)
            return (
                cos(angle) * remaining + x1,
                sin(angle) * remaining + y1,
                angle)
        total_length += segment_length
    if first_segment is None:
        # Zero-length path: there is nowhere to put glyphs.
        return None
    if width <= 0:
        x1, y1, x2, y2 = first_segment
        return x1, y1, point_angle(x1, y1, x2, y2)
    # ``width`` is beyond the end of the path: stay on the last point,
    # keeping the last known direction.
    x1, y1, x2, y2 = last_segment
    return x2, y2, point_angle(x1, y1, x2, y2)


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

    text_anchor = node.get('text-anchor')
    if text_anchor == 'middle':
        x_align = - (width / 2 + x_bearing)
        if letter_spacing and node.text:
            x_align -= (len(node.text) - 1) * letter_spacing / 2
    elif text_anchor == 'end':
        x_align = - (width + x_bearing)
        if letter_spacing and node.text:
            x_align -= (len(node.text) - 1) * letter_spacing

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
    # Layout along a path is local to the textPath: a text that does not
    # fit must not leave a cumulative offset for sibling text nodes.
    text_path_width = surface.text_path_width
    if text_path:
        surface.context.new_path()
        surface.stroke_and_fill = False
        surface.draw(text_path)
        surface.stroke_and_fill = True
        cairo_path = surface.context.copy_path_flat()
        surface.context.new_path()
        path_total_length = path_length(cairo_path)
        # Percentages in startOffset are relative to the path length, as
        # required by the SVG 1.1 textPath specification.
        start_offset = size(
            surface, node.get('startOffset', 0), path_total_length)
        if start_offset < 0:
            start_offset = 0
        elif start_offset > path_total_length:
            start_offset = path_total_length
        if node.tag == 'textPath':
            text_path_width += start_offset
        text_path_width += x_align
        bounding_box = extend_bounding_box(
            bounding_box, ((start_offset, 0),))

    if node.text:
        for i, ((x, y, dx, dy, r), letter) in enumerate(letters_positions):
            if x:
                surface.cursor_d_position[0] = 0
            if y:
                surface.cursor_d_position[1] = 0
            surface.cursor_d_position[0] += dx or 0
            surface.cursor_d_position[1] += dy or 0
            text_extents = surface.context.text_extents(letter)
            extents = text_extents[4]
            if cairo_path is not None and path_total_length > 0:
                start = text_path_width + surface.cursor_d_position[0]
                middle = start + extents / 2
                end = start + extents
                if i:
                    extents += letter_spacing
                text_path_width += extents
                # Glyphs whose middle is outside the path are not drawn.
                if not 0 <= middle <= path_total_length:
                    continue
                start_location = point_following_path(cairo_path, start)
                middle_location = point_following_path(cairo_path, middle)
                end_location = point_following_path(cairo_path, end)
                if (start_location is None or middle_location is None
                        or end_location is None):
                    continue
                start_point = start_location[:2]
                # Use the tangent at the glyph middle: short segments and
                # clamped end points would otherwise give a flat angle.
                angle = middle_location[2]
                surface.context.save()
                surface.context.translate(*start_point)
                surface.context.rotate(angle)
                surface.context.translate(0, surface.cursor_d_position[1])
                surface.context.move_to(0, 0)
                bounding_box = extend_bounding_box(
                    bounding_box,
                    ((end_location[0], text_extents[3]),))
            elif cairo_path is not None:
                # A zero-length referenced path: no glyph can be laid out,
                # and this state must not leak to the following text.
                continue
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
            if cairo_path is None:
                surface.cursor_position = cursor_position
    else:
        x = x[0] if x else surface.cursor_position[0]
        y = y[0] if y else surface.cursor_position[1]
        dx = dx[0] if dx else 0
        dy = dy[0] if dy else 0
        surface.cursor_position = (x + dx, y + dy)

    if text_path:
        # Publish the local advance so that nested tspans of the same
        # textPath continue where this node stopped. Sibling ``text``
        # nodes are reset in surface.draw, so the state cannot leak out.
        surface.text_path_width = text_path_width

    # If a valid bounding box is calculated store it in the node
    if is_valid_bounding_box(bounding_box):
        node['text_bounding_box'] = bounding_box
