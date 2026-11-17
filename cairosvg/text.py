"""
Text drawers.

Text is normally drawn one ``text``/``tspan`` node at a time, advancing the
surface cursor as glyphs are emitted.  When an author asks for a specific
width with ``textLength`` (or when ``letter-spacing`` has to stay continuous
across ``tspan`` boundaries), the whole text run is laid out first by
:mod:`cairosvg.text_layout` and each node then paints its share of that
layout.

Supported scope
---------------

* ``textLength`` on ``text`` and ``tspan`` elements, with ``lengthAdjust``
  ``spacing`` (default) and ``spacingAndGlyphs``.
* Lengths with units (``px``, ``pt``, ``mm``, …), ``em``/``ex``/``ch`` and
  percentages of the viewport width.  Zero, negative and invalid values are
  ignored, matching the "negative value is an error" rule of SVG 1.1.
* Nested ``tspan``/``a`` elements, whose text shares a single layout with
  its ancestors, so ``letter-spacing`` and calibration are continuous.
* A nested ``tspan`` with its own ``textLength`` is calibrated first and
  then treated as one indivisible block by its ancestors.
* Explicit ``x``/``y`` lists (which pin characters and start a new anchored
  chunk), ``dx``/``dy`` and ``rotate`` lists.
* ``text-anchor`` (``start``/``middle``/``end``) applied per anchored chunk
  to the calibrated width, and the vertical ``display-anchor`` /
  ``dominant-baseline`` handling that already existed.

Out of scope: ``textPath`` (it keeps the historical layout), automatic
wrapping, bidirectional reordering, and kerning/ligature substitution.

Known error sources and browser differences
-------------------------------------------

* Glyph advances are read from Cairo's toy text API, which applies no
  kerning across characters.  A browser with kerning enabled can therefore
  differ by a small fraction of an em on kerned pairs.
* The *ink* box measured in a PNG is narrower than the calibrated advance
  box by the first glyph's left side bearing and the last glyph's right side
  bearing, plus, with ``letter-spacing``, the trailing spacing after the last
  character.  Tests derive those values from the same font instead of
  hard-coding pixels.
* Chrome and WebKit disagree on a nested ``tspan`` with ``textLength``:
  Chrome shifts the content after the block, WebKit ignores the nested
  attribute.  This implementation follows the SVG 2 procedure (calibrate the
  block, then continue after its calibrated width), which matches Chrome for
  the start of the following characters.
* Hinting can make Cairo's reported advance differ from the hinted glyph
  outline by a fraction of a pixel; the PNG edge is therefore allowed a
  small tolerance in tests.
"""

from math import cos, radians, sin

from .bounding_box import (
    EMPTY_BOUNDING_BOX, extend_bounding_box, is_valid_bounding_box)
from .helpers import distance, normalize, point_angle, size, zip_letters
from .surface import cairo
from .text_layout import (
    build_layout, cached_font_size, cached_layout, is_text_root,
    needs_layout)
from .url import parse_url


def path_length(path):
    """Get the length of ``path``."""
    total_length = 0
    for item in path:
        if item[0] == cairo.PATH_MOVE_TO:
            old_point = item[1]
        elif item[0] == cairo.PATH_LINE_TO:
            new_point = item[1]
            length = distance(
                old_point[0], old_point[1], new_point[0], new_point[1])
            total_length += length
            old_point = new_point
    return total_length


def point_following_path(path, width):
    """Get the point at ``width`` distance on ``path``."""
    total_length = 0
    for item in path:
        if item[0] == cairo.PATH_MOVE_TO:
            old_point = item[1]
        elif item[0] == cairo.PATH_LINE_TO:
            new_point = item[1]
            length = distance(
                old_point[0], old_point[1], new_point[0], new_point[1])
            total_length += length
            if total_length < width:
                old_point = new_point
            else:
                length -= total_length - width
                angle = point_angle(
                    old_point[0], old_point[1], new_point[0], new_point[1])
                x = cos(angle) * length + old_point[0]
                y = sin(angle) * length + old_point[1]
                return x, y


def text(surface, node, draw_as_text=False):
    """Draw a text ``node``."""
    if not draw_as_text and is_text_root(node) and needs_layout(surface, node):
        # Build the layout once, when the shape pass starts; the fill pass
        # and the descendant ``tspan`` nodes reuse it.
        build_layout(surface, node)
    layout = cached_layout(surface, node)
    if layout is not None:
        _draw_layout(surface, node, layout, draw_as_text)
        return

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
    if text_path:
        surface.context.new_path()
        surface.stroke_and_fill = False
        surface.draw(text_path)
        surface.stroke_and_fill = True
        cairo_path = surface.context.copy_path_flat()
        surface.context.new_path()
        length = path_length(cairo_path) + x_bearing
        start_offset = size(surface, node.get('startOffset', 0), length)
        if node.tag == 'textPath':
            surface.text_path_width += start_offset
        surface.text_path_width += x_align
        bounding_box = extend_bounding_box(bounding_box, ((start_offset, 0),))

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
            if text_path:
                start = surface.text_path_width + surface.cursor_d_position[0]
                start_point = point_following_path(cairo_path, start)
                middle = start + extents / 2
                middle_point = point_following_path(cairo_path, middle)
                end = start + extents
                end_point = point_following_path(cairo_path, end)
                if i:
                    extents += letter_spacing
                surface.text_path_width += extents
                if not all((start_point, middle_point, end_point)):
                    continue
                if not 0 <= middle <= length:
                    continue
                surface.context.save()
                surface.context.translate(*start_point)
                surface.context.rotate(point_angle(*(start_point + end_point)))
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
            if not text_path:
                surface.cursor_position = cursor_position
    else:
        x = x[0] if x else surface.cursor_position[0]
        y = y[0] if y else surface.cursor_position[1]
        dx = dx[0] if dx else 0
        dy = dy[0] if dy else 0
        surface.cursor_position = (x + dx, y + dy)

    # If a valid bounding box is calculated store it in the node
    if is_valid_bounding_box(bounding_box):
        node['text_bounding_box'] = bounding_box


def _draw_layout(surface, node, characters, draw_as_text):
    """Paint characters previously laid out by :mod:`cairosvg.text_layout`."""
    from .text_layout import select_font

    font_size = _layout_font_size(surface, node)
    select_font(surface, node, font_size)
    old_font_size = surface.font_size
    surface.font_size = font_size
    try:
        bounding_box = EMPTY_BOUNDING_BOX
        for character in characters:
            if character.char.isspace():
                continue
            extents = character.extents
            x_bearing, y_bearing, width, height = extents[:4]
            scale = character.scale
            surface.context.save()
            surface.context.translate(character.x, character.y)
            surface.context.rotate(radians(character.rotate or 0))
            if scale != 1:
                surface.context.scale(scale, 1)
            surface.context.move_to(0, 0)
            if draw_as_text:
                surface.context.show_text(character.char)
            else:
                surface.context.text_path(character.char)
            surface.context.restore()
            left = character.x + x_bearing * scale
            right = character.x + (x_bearing + width) * scale
            top = character.y + y_bearing
            bottom = top + height
            if character.rotate:
                left, top, right, bottom = _rotated_box(
                    character.x, character.y, left, top, right, bottom,
                    character.rotate)
            bounding_box = extend_bounding_box(
                bounding_box, ((left, top), (right, bottom)))
        if is_valid_bounding_box(bounding_box):
            node['text_bounding_box'] = bounding_box
        if characters:
            last = characters[-1]
            surface.cursor_position = [last.end, last.y]
    finally:
        surface.font_size = old_font_size


def _rotated_box(cx, cy, left, top, right, bottom, angle):
    """Return the axis-aligned box of a rectangle rotated about a point."""
    angle = radians(angle)
    cosinus, sinus = cos(angle), sin(angle)
    xs, ys = [], []
    for x, y in ((left, top), (right, top), (left, bottom), (right, bottom)):
        dx, dy = x - cx, y - cy
        xs.append(cx + dx * cosinus - dy * sinus)
        ys.append(cy + dx * sinus + dy * cosinus)
    return min(xs), min(ys), max(xs), max(ys)


def _layout_font_size(surface, node):
    """Return the font size used when ``node`` was laid out."""
    font_size = cached_font_size(surface, node)
    return surface.font_size if font_size is None else font_size
