"""
Text drawers.

"""

from math import cos, radians, sin

from .bounding_box import (
    EMPTY_BOUNDING_BOX, extend_bounding_box, is_valid_bounding_box)
from .helpers import (
    distance, normalize, point_angle, rotate, size, zip_letters)
from .surface import cairo
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


def _values(surface, node, attribute, reference):
    """Parse a space-separated list of lengths on ``node``."""
    if attribute not in node:
        return []
    return [
        size(surface, value, reference)
        for value in normalize(node[attribute]).strip().split(' ')]


def _rotation_values(node):
    """Parse the ``rotate`` attribute of ``node``."""
    if 'rotate' not in node:
        return []
    return [
        radians(float(value)) if value else 0
        for value in normalize(node['rotate']).strip().split(' ')]


def _select_font(surface, node):
    """Configure the cairo context with the font properties of ``node``."""
    font_family = (
        (node.get('font-family') or 'sans-serif').split(',')[0]
        .strip('"\' '))
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
    # Resolve the node's own size: during the internal subtree walk the
    # surface-wide font_size still belongs to an ancestor, so reading the
    # attribute here keeps per-tspan sizes and weights effective.
    surface.context.set_font_size(size(surface, node.get('font-size', '12pt')))


def _vertical_align(surface, node, ascent, descent):
    """Return the vertical (display/baseline) alignment offset."""
    # TODO: This is a hack. The rest of the baseline alignment tags of the SVG
    # 1.1 spec (section 10.9.2) are not supported. We only try to align things
    # that look like Western horizontal fonts.
    # Finally, we add a "display-anchor" attribute for aligning the specific
    # text rather than the font baseline.
    # Nonetheless, there are times when one needs to align text vertically, and
    # this will at least make that possible.
    _, _, _, max_x_advance, max_y_advance = surface.context.font_extents()
    y_align = 0
    if max_x_advance > 0 and max_y_advance == 0:
        display_anchor = node.get('display-anchor')
        alignment_baseline = (node.get('dominant-baseline') or
                              node.get('alignment-baseline'))
        x_bearing, y_bearing, _, height = (
            surface.context.text_extents(node.text or '')[:4])
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
    return y_align


def _layout_text(surface, node):
    """Walk the text subtree and lay every glyph out in independent chunks.

    A new chunk starts whenever an absolute ``x`` (or ``y``) position is set,
    either through a position list on the current node or an attribute on a
    child node. ``dx``/``dy`` are relative offsets accumulated across the
    subtree and reset on the matching axis whenever an absolute position
    overrides it, so parent and child coordinates are never applied twice.

    Return a list of chunks; each chunk is a dict with the anchor of its first
    glyph and the list of its glyphs.

    """
    chunks = []
    # Absolute cursor position and accumulated relative offsets. The two
    # axes are independent: an absolute x only clears dx, an absolute y only
    # clears dy (SVG 1.1, section 10.5).
    # x/y/dx/dy are not inherited by child tspans. When the root element has
    # no text of its own (its content lives in children), seed the leading
    # root position so the first child glyph starts there instead of at 0.
    seed_position = not (node.text or '')
    root_x = _values(surface, node, 'x', 'x') if seed_position else []
    root_y = _values(surface, node, 'y', 'y') if seed_position else []
    root_dx = _values(surface, node, 'dx', 'x') if seed_position else []
    root_dy = _values(surface, node, 'dy', 'y') if seed_position else []
    state = {
        'x': root_x[0] if root_x else 0.,
        'y': root_y[0] if root_y else 0.,
        'dx': root_dx[0] if root_dx else 0.,
        'dy': root_dy[0] if root_dy else 0.,
        'rotate': 0.,
        'index': 0,
    }

    def walk(current):
        _select_font(surface, current)
        ascent, descent = surface.context.font_extents()[:2]
        letter_spacing = size(surface, current.get('letter-spacing'))
        y_align = _vertical_align(surface, current, ascent, descent)
        text_anchor = current.get('text-anchor')
        x_list = _values(surface, current, 'x', 'x')
        y_list = _values(surface, current, 'y', 'y')
        dx_list = _values(surface, current, 'dx', 'x')
        dy_list = _values(surface, current, 'dy', 'y')
        rotate_list = _rotation_values(current)
        positions = zip_letters(
            list(x_list), list(y_list), list(dx_list), list(dy_list),
            list(rotate_list), current.text or '')

        for (x_value, y_value, dx_value, dy_value, rotation), letter in \
                positions:
            glyph_index = state['index']
            state['index'] += 1

            # An absolute position starts a new chunk and drops the relative
            # offset accumulated on the same axis (SVG 1.1, 10.5).
            new_chunk = x_value is not None or y_value is not None
            if x_value is not None:
                state['x'] = x_value
                state['dx'] = 0.
            if y_value is not None:
                state['y'] = y_value
                state['dy'] = 0.

            # Relative offsets are cumulative across the text subtree until
            # an absolute position of the same axis resets them.
            state['dx'] += dx_value or 0.
            state['dy'] += dy_value or 0.
            if rotation is not None:
                state['rotate'] = rotation

            extents = surface.context.text_extents(letter)

            # letter-spacing inserts extra space before every glyph but the
            # first one of the whole text element (SVG 1.1, 10.10).
            spacing = letter_spacing if glyph_index else 0.
            if spacing:
                state['x'] += spacing

            if not chunks or new_chunk:
                chunks.append({
                    'anchor': text_anchor,
                    'glyphs': [],
                    'width': 0.,
                    'x': state['x'],
                    'y': state['y'],
                })
            chunk = chunks[-1]
            # An anchor defined on a child node only applies to the chunk the
            # child starts; the chunk keeps the first anchor it was given.
            if chunk['anchor'] is None:
                chunk['anchor'] = text_anchor

            glyph = {
                'letter': letter,
                'x': state['x'] + state['dx'],
                'y': state['y'] + state['dy'],
                'rotation': state['rotate'],
                'y_align': y_align,
                'spacing': spacing,
                'extents': extents,
                'node': current,
            }
            chunk['glyphs'].append(glyph)
            chunk['width'] += extents[4] + spacing

            # Advance the current text position for the following glyphs.
            state['x'] += extents[4]

        for child in current.children:
            walk(child)

    walk(node)

    # Resolve text-anchor per chunk: the shift applies to the whole chunk,
    # independently of the other chunks of the same text element.
    for chunk in chunks:
        anchor = chunk.get('anchor')
        if anchor == 'middle':
            chunk['x_align'] = -chunk['width'] / 2.
        elif anchor == 'end':
            chunk['x_align'] = -chunk['width']
        else:
            chunk['x_align'] = 0.
    return chunks


def _draw_plain_text(surface, node, draw_as_text):
    """Draw the glyphs of one plain text node from the shared chunk layout.

    The whole subtree is laid out once by its root; the resulting glyphs are
    grouped per source node so every node is painted with its own fill and
    stroke when ``surface.draw`` reaches it.

    """
    chunks = surface.text_layout
    bounding_box = EMPTY_BOUNDING_BOX

    for chunk in chunks:
        x_align = chunk['x_align']
        for glyph in chunk['glyphs']:
            if glyph['node'] is not node:
                continue
            letter = glyph['letter']
            _select_font(surface, glyph['node'])
            x_bearing, y_bearing, width, height, x_advance, _ = (
                glyph['extents'])
            surface.context.save()
            surface.context.move_to(
                glyph['x'] + x_align, glyph['y'] + glyph['y_align'])
            surface.context.rotate(glyph['rotation'])
            if glyph['rotation']:
                corners = (
                    (x_bearing, y_bearing),
                    (x_bearing + width, y_bearing),
                    (x_bearing + width, y_bearing + height),
                    (x_bearing, y_bearing + height))
                points = [
                    rotate(cx, cy, glyph['rotation']) for cx, cy in corners]
                box_x = min(point[0] for point in points) + glyph['x'] + x_align
                box_y = min(point[1] for point in points) + glyph['y'] + \
                    glyph['y_align']
                box_x2 = max(point[0] for point in points) + glyph['x'] + \
                    x_align
                box_y2 = max(point[1] for point in points) + glyph['y'] + \
                    glyph['y_align']
                bounding_box = extend_bounding_box(
                    bounding_box, ((box_x, box_y), (box_x2, box_y2)))
            else:
                points = (
                    (glyph['x'] + x_align,
                     glyph['y'] + glyph['y_align']),
                    (glyph['x'] + x_align + x_advance,
                     glyph['y'] + glyph['y_align'] + height))
                bounding_box = extend_bounding_box(bounding_box, points)

            # Only draw characters with 'content' (workaround for bug in cairo)
            if not letter.isspace():
                if draw_as_text:
                    surface.context.show_text(letter)
                else:
                    surface.context.text_path(letter)
            surface.context.restore()

    if is_valid_bounding_box(bounding_box):
        # Merge into the root's bounding box so the whole text element keeps a
        # single combined box even though each node paints independently.
        root_box = node.get('text_bounding_box')
        if root_box:
            node['text_bounding_box'] = extend_bounding_box(
                root_box, (
                    (bounding_box[0], bounding_box[1]),
                    (bounding_box[0] + bounding_box[2],
                     bounding_box[1] + bounding_box[3])))
        else:
            node['text_bounding_box'] = bounding_box


def _has_text_path(node):
    """Tell whether a text subtree references a ``textPath`` element."""
    if node.tag == 'textPath':
        return True
    return any(_has_text_path(child) for child in node.children)


def _draw_text_on_path(surface, node, draw_as_text):
    """Draw a text subtree containing a textPath with the legacy engine."""
    for text_node in _iter_text_nodes(node):
        _draw_text_on_path_node(surface, text_node, draw_as_text)


def _iter_text_nodes(node):
    """Yield a node and its text-bearing descendants in document order."""
    yield node
    for child in node.children:
        yield from _iter_text_nodes(child)


def text(surface, node, draw_as_text=False):
    """Draw a text ``node``."""
    # Plain text lays out its whole subtree once when its root is reached and
    # stores the per-node glyph groups on the surface. Every node then draws
    # only its own glyphs when surface.draw reaches it, so it is painted with
    # its own fill and stroke. The shared layout is keyed on the surface and
    # released by surface.draw when the root finishes, which keeps each text
    # element independent of previous cursor state and the tree reusable.
    if _has_text_path(node):
        # The legacy on-path engine walks and draws its own subtree; set the
        # active path root so surface.draw skips its normal child traversal
        # and the fill/stroke re-entry uses the legacy redraw path.
        surface.text_uses_path_engine = True
        surface.text_path_root = node
        _draw_text_on_path(surface, node, draw_as_text)
        return

    if surface.text_layout is None:
        # Root of a plain text subtree: compute the whole layout once.
        surface.text_layout = _layout_text(surface, node)
        surface.text_layout_root = node

    # Every node (root included) emits its own glyph paths on its own draw
    # pass; surface.draw then applies that node's fill and stroke. Nodes
    # without text simply contribute no path.
    _draw_plain_text(surface, node, draw_as_text)


def _draw_text_on_path_node(surface, node, draw_as_text):
    """Draw one text/tspan node following a path: legacy per-node code."""
    _select_font(surface, node)
    ascent, descent, _, max_x_advance, max_y_advance = (
        surface.context.font_extents())

    text_path_href = parse_url(
        node.get_href() or node.parent.get_href() or '')
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
