"""
Text drawers.

"""

from math import cos, radians, sin

from .bounding_box import (
    EMPTY_BOUNDING_BOX, extend_bounding_box, is_valid_bounding_box)
from .helpers import distance, normalize, point_angle, size
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


def _numbers(surface, node, attribute, reference):
    """Parse a space-separated SVG position attribute."""
    if attribute not in node:
        return []
    value = normalize(node[attribute]).strip()
    if not value:
        return []
    return [size(surface, item, reference) for item in value.split(' ')]


def _rotations(node):
    if 'rotate' not in node:
        return []
    value = normalize(node['rotate']).strip()
    if not value:
        return []
    return [radians(float(item)) if item else 0
            for item in value.split(' ')]


class TextState:
    """Shared layout state for one ``text`` element during one pass.

    A fresh state is created for every root ``text`` node and for every render
    pass (text path, then fill), so positions never depend on text drawn
    previously. It replaces the old global ``cursor_position`` /
    ``cursor_d_position`` mutation for plain text, keeping all state local to
    the element being laid out.

    Text is split into *chunks* per SVG 1.1 §10.5: every explicit ``x`` or
    ``y`` value starts a new chunk, and ``text-anchor`` is applied to the
    whole chunk rather than repeated on every glyph.

    """

    def __init__(self, surface, root, letter_spacing):
        self.surface = surface
        self.root = root
        self.letter_spacing = letter_spacing
        # The single current text position (SVG 1.1 §10.5). Explicit x/y set
        # it absolutely; supplemental dx/dy move it relatively; glyph advances
        # move it on after each glyph. Because every displacement is folded
        # into this one position, an offset is applied exactly once and never
        # re-added by a nested or sibling span.
        self.cursor = [0.0, 0.0]
        # Each chunk: {'anchor', 'origin_x', 'origin_y', 'glyphs'}
        self.chunks = []
        self._font_key = None
        self._extents_cache = {}
        # Build the whole subtree layout up front. The surface then visits
        # the same nodes again for stroking and filling; the cached geometry
        # is reused, so both passes get identical glyph positions.
        self._build_subtree(root)

    def _build_subtree(self, node, inherited_anchor=None):
        """Lay out ``node``'s own text, then recurse into its children.

        ``text-anchor`` is an inherited property: a span without the
        attribute keeps the anchor in force on its parent. Explicit ``x``/``y``
        positions opening a new chunk therefore use the *effective* anchor,
        not an implicit 'start'.

        The current text position (``cursor``) is shared across the whole text
        element, which is exactly how SVG 1.1 defines it: a supplemental
        displacement made by one node persists for the following glyphs and
        descends into child spans, but the node's dx/dy attribute is read only
        when that node is processed, so it is never inherited or doubled.
        """
        anchor = node.get('text-anchor') or inherited_anchor or 'start'
        self.layout_node(node, anchor)
        for child in node.children:
            self._build_subtree(child, anchor)

    def select_font(self, node):
        """Select the node's font on the context; return a cache key."""
        font_family = (
            (node.get('font-family') or 'sans-serif')
            .split(',')[0].strip('"\' '))
        font_style = getattr(
            cairo, f'font_slant_{node.get("font-style")}'.upper(),
            cairo.FONT_SLANT_NORMAL)
        node_font_weight = node.get('font-weight')
        if (node_font_weight and node_font_weight.isdigit()
                and int(node_font_weight) >= 550):
            node_font_weight = 'bold'
        font_weight = getattr(
            cairo, f'font_weight_{node_font_weight}'.upper(),
            cairo.FONT_WEIGHT_NORMAL)
        font_size = size(self.surface, node.get('font-size', '12pt'))
        key = (font_family, font_style, font_weight, font_size)
        if key != self._font_key:
            self.surface.context.select_font_face(
                font_family, font_style, font_weight)
            self.surface.context.set_font_size(font_size)
            self._font_key = key
        return key

    def extents(self, node, letter):
        """Cached cairo text extents for ``letter`` in ``node``'s font."""
        key = self.select_font(node)
        cache = self._extents_cache.setdefault(key, {})
        if letter not in cache:
            cache[letter] = self.surface.context.text_extents(letter)
        return cache[letter]

    def layout_node(self, node, anchor):
        """Lay out the text directly held by ``node``.

        Child ``tspan`` nodes are handled by :meth:`_build_subtree`; here we
        only consume ``node.text``. Position attributes (x/y/dx/dy/rotate)
        index the characters of this node only, matching SVG 1.1 §10.5.
        """
        x_values = _numbers(self.surface, node, 'x', 'x')
        y_values = _numbers(self.surface, node, 'y', 'y')
        dx_values = _numbers(self.surface, node, 'dx', 'x')
        dy_values = _numbers(self.surface, node, 'dy', 'y')
        rotate_values = _rotations(node)
        last_rotation = rotate_values[-1] if rotate_values else None
        text = node.text or ''

        for index, letter in enumerate(text):
            x = x_values[index] if index < len(x_values) else None
            y = y_values[index] if index < len(y_values) else None
            dx = dx_values[index] if index < len(dx_values) else 0
            dy = dy_values[index] if index < len(dy_values) else 0
            rotation = (rotate_values[index]
                        if index < len(rotate_values) else last_rotation)
            self._place_glyph(node, letter, x, y, dx, dy, rotation, anchor)

        if not text:
            # An empty element still moves the current text position through
            # its first x/y/dx/dy values (SVG 1.1 §10.5), without painting.
            if x_values:
                self.cursor[0] = x_values[0]
            if y_values:
                self.cursor[1] = y_values[0]
            if dx_values:
                self.cursor[0] += dx_values[0]
            if dy_values:
                self.cursor[1] += dy_values[0]

    def _place_glyph(self, node, letter, x, y, dx, dy, rotation, anchor):
        # An explicit absolute coordinate sets the current point and opens a
        # new chunk boundary on that axis.
        starts_chunk = False
        if x is not None:
            self.cursor[0] = x
            starts_chunk = True
        if y is not None:
            self.cursor[1] = y
            starts_chunk = True
        # Supplemental displacement moves the current point exactly once.
        self.cursor[0] += dx
        self.cursor[1] += dy

        pen_x, pen_y = self.cursor

        if starts_chunk or not self.chunks:
            chunk = {
                'anchor': anchor,
                'origin_x': pen_x,
                'origin_y': pen_y,
                'glyphs': [],
            }
            self.chunks.append(chunk)
        else:
            chunk = self.chunks[-1]

        x_bearing, y_bearing, width, height, x_advance, y_advance = (
            self.extents(node, letter))
        chunk['glyphs'].append({
            'letter': letter,
            'node': node,
            'x': pen_x,
            'y': pen_y,
            'rotation': rotation,
            'x_bearing': x_bearing,
            'y_bearing': y_bearing,
            'width': width,
            'height': height,
            'x_advance': x_advance,
            'y_advance': y_advance,
            'y_align': vertical_alignment(self.surface, node, letter),
        })

        # Advance the current text position past the glyph. Letter spacing is
        # added after every glyph; an explicit position on the next glyph
        # overrides it.
        self.cursor = [pen_x + x_advance, pen_y + y_advance]
        if self.letter_spacing:
            self.cursor[0] += self.letter_spacing

    def chunk_shift(self, chunk):
        """Horizontal pen shift implementing the chunk's ``text-anchor``.

        Ink extents are computed in absolute user coordinates (each glyph's
        pen position plus its bearing/width), so the shift reflects the whole
        chunk rather than only its first glyph. It is applied once per chunk,
        so an anchor is never repeated on every glyph and a mid-line ``x``
        change centers only the chunk it opens.

        """
        glyphs = chunk['glyphs']
        min_x = min(g['x'] + g['x_bearing'] for g in glyphs)
        max_x = max(g['x'] + g['x_bearing'] + g['width'] for g in glyphs)
        if chunk['anchor'] == 'middle':
            return chunk['origin_x'] - (min_x + max_x) / 2
        if chunk['anchor'] == 'end':
            return chunk['origin_x'] - max_x
        return 0

    def render(self, draw_as_text):
        """Draw all chunks; return the bounding box in user coordinates."""
        context = self.surface.context
        bounding_box = EMPTY_BOUNDING_BOX
        for chunk in self.chunks:
            shift = self.chunk_shift(chunk)
            for glyph in chunk['glyphs']:
                self.select_font(glyph['node'])
                context.save()
                # The glyph's stored x is already absolute; the chunk's
                # anchor shift is applied once to every glyph in the chunk.
                pen_x = glyph['x'] + shift
                pen_y = glyph['y']
                context.move_to(pen_x, pen_y)
                if glyph['rotation'] is not None:
                    context.rotate(glyph['rotation'])
                if glyph['y_align']:
                    context.rel_move_to(0, glyph['y_align'])
                bounding_box = extend_bounding_box(
                    bounding_box,
                    self._corners(glyph, pen_x, pen_y))
                if not glyph['letter'].isspace():
                    if draw_as_text:
                        context.show_text(glyph['letter'])
                    else:
                        context.text_path(glyph['letter'])
                context.restore()
        return bounding_box

    @staticmethod
    def _corners(glyph, pen_x, pen_y):
        x_bearing = glyph['x_bearing']
        y_bearing = glyph['y_bearing'] + glyph['y_align']
        angle = glyph['rotation'] or 0
        cos_a, sin_a = cos(angle), sin(angle)
        corners = []
        for dx, dy in (
                (x_bearing, y_bearing),
                (x_bearing + glyph['width'], y_bearing),
                (x_bearing, y_bearing + glyph['height']),
                (x_bearing + glyph['width'], y_bearing + glyph['height'])):
            corners.append((
                pen_x + dx * cos_a - dy * sin_a,
                pen_y + dx * sin_a + dy * cos_a))
        return corners


def vertical_alignment(surface, node, letter):
    """Vertical pen offset for the node's baseline alignment properties."""
    context = surface.context
    ascent, descent, _, max_x_advance, max_y_advance = context.font_extents()
    _x_bearing, y_bearing, _width, height = context.text_extents(letter)[:4]
    y_align = 0
    # The SVG 1.1 baseline alignment properties are only approximated for
    # Western-style horizontal fonts, where cairo exposes sensible extents.
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
        elif alignment_baseline in ('central', 'middle'):
            # TODO: cairo exposes no x-height, so the full font top-to-bottom
            # extent is used as an approximation.
            y_align = (ascent + descent) / 2 - descent
        elif alignment_baseline in (
                'text-before-edge', 'before_edge', 'top',
                'hanging', 'text-top'):
            y_align = ascent
        elif alignment_baseline in (
                'text-after-edge', 'after_edge', 'bottom', 'text-bottom'):
            y_align = -descent
    return y_align


def text(surface, node, draw_as_text=False):
    """Draw a text ``node`` (also used for ``tspan``, ``a`` and textPath)."""
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

    letter_spacing = size(surface, node.get('letter-spacing'))

    text_path_href = parse_url(node.get_href() or node.parent.get_href() or '')
    text_path = (
        surface.paths.get(text_path_href.fragment)
        if text_path_href.fragment else None)

    if text_path:
        _draw_text_path(
            surface, node, letter_spacing, text_path, draw_as_text)
        return

    # Plain text is laid out and drawn for the whole subtree when the root
    # 'text' node is visited. The surface then reaches the descendant tspan/a
    # nodes through its own child traversal; their geometry is already in the
    # cached layout, so they must not lay themselves out a second time (which
    # would re-apply dx/dy and re-open chunks with a reset cursor).
    if node.tag != 'text':
        return

    # A fresh, self-contained state per root node and per pass (text path,
    # then fill). Nothing here depends on text drawn previously.
    state = TextState(surface, node, letter_spacing)
    bounding_box = state.render(draw_as_text)
    if is_valid_bounding_box(bounding_box):
        node['text_bounding_box'] = bounding_box


def _draw_text_path(surface, node, letter_spacing, text_path, draw_as_text):
    """Render glyphs following a path (SVG textPath)."""
    x_bearing, _y_bearing, width, _height = (
        surface.context.text_extents(node.text)[:4])

    x = _numbers(surface, node, 'x', 'x')
    y = _numbers(surface, node, 'y', 'y')
    dx = _numbers(surface, node, 'dx', 'x')
    dy = _numbers(surface, node, 'dy', 'y')
    rotate = _rotations(node) or [0]
    last_r = rotate[-1]
    letters_positions = _zip_letters(x, y, dx, dy, rotate, node.text)

    x_align = 0
    text_anchor = node.get('text-anchor')
    if text_anchor == 'middle':
        x_align = - (width / 2 + x_bearing)
        if letter_spacing and node.text:
            x_align -= (len(node.text) - 1) * letter_spacing / 2
    elif text_anchor == 'end':
        x_align = - (width + x_bearing)
        if letter_spacing and node.text:
            x_align -= (len(node.text) - 1) * letter_spacing

    surface.context.new_path()
    saved_flag = surface.stroke_and_fill
    surface.stroke_and_fill = False
    surface.draw(text_path)
    surface.stroke_and_fill = saved_flag
    cairo_path = surface.context.copy_path_flat()
    surface.context.new_path()
    length = path_length(cairo_path) + x_bearing
    start_offset = size(surface, node.get('startOffset', 0), length)
    if node.tag == 'textPath':
        surface.text_path_width += start_offset
    surface.text_path_width += x_align
    bounding_box = extend_bounding_box(
        EMPTY_BOUNDING_BOX, ((start_offset, 0),))

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
            if not letter.isspace():
                if draw_as_text:
                    surface.context.show_text(letter)
                else:
                    surface.context.text_path(letter)
            surface.context.restore()
    else:
        x_value = x[0] if x else surface.cursor_position[0]
        y_value = y[0] if y else surface.cursor_position[1]
        dx_value = dx[0] if dx else 0
        dy_value = dy[0] if dy else 0
        surface.cursor_position = (x_value + dx_value, y_value + dy_value)

    if is_valid_bounding_box(bounding_box):
        node['text_bounding_box'] = bounding_box


def _zip_letters(xl, yl, dxl, dyl, rl, word):
    """Pair each character with the first remaining position-list values."""
    return (
        ([pl.pop(0) if pl else None for pl in (xl, yl, dxl, dyl, rl)], char)
        for char in word)
