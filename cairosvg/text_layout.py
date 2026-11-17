"""
Text length and letter spacing layout.

This module lays out a whole ``text`` subtree when an author asks for a
specific width with ``textLength``, or when ``letter-spacing`` has to be
applied continuously across ``tspan`` boundaries.

The order of operations follows the SVG 2 text layout algorithm:

1. every character is measured with the same font that will paint it;
2. ``dx``/``dy`` and ``letter-spacing`` place characters from the current
   text position, ignoring ``x`` and ``y``;
3. ``textLength`` calibrates the run, descendants first (a descendant with
   its own ``textLength`` is then an indivisible block for its ancestors);
4. explicit ``x`` and ``y`` values pin characters and shift what follows;
5. ``text-anchor`` is applied to each anchored chunk.

See :mod:`cairosvg.text` for the supported scope, the sources of error and
the points where browsers are known to disagree.
"""

import re
from math import isfinite

from .helpers import normalize, size
from .surface import cairo

TEXT_TAGS = frozenset(('text', 'tspan', 'textPath', 'a'))

LENGTH_RE = re.compile(
    r'^(?P<number>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)'
    r'(?P<unit>%|em|ex|ch|px|mm|cm|in|pt|pc)?$')


def is_text_root(node):
    """Return whether ``node`` starts a text run in the SVG tree."""
    if node.tag not in ('text', 'a'):
        return False
    parent = node.parent
    while parent is not None:
        if parent.tag in TEXT_TAGS:
            return False
        parent = parent.parent
    return True


def parse_text_length(surface, node):
    """Return a usable ``textLength`` value for ``node``, or ``None``.

    Invalid, negative and non-finite values are ignored, as SVG does with a
    negative length.  Percentages are resolved against the viewport width.

    """
    value = node.get('textLength')
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    match = LENGTH_RE.match(value)
    if match is None:
        return None
    try:
        number = float(match.group('number'))
    except ValueError:
        return None
    if not isfinite(number) or number <= 0:
        # A zero length cannot be reached by distributing gaps or by
        # scaling, and browsers keep the natural layout; treat it as invalid.
        return None
    if match.group('unit') == '%':
        return number * (surface.context_width or 0) / 100
    return size(surface, value, 'x')


def length_adjust(node):
    """Return the kind of adjustment requested by ``node``."""
    return (
        'spacingAndGlyphs'
        if node.get('lengthAdjust') == 'spacingAndGlyphs' else 'spacing')


def parse_length_list(surface, node, attribute, reference):
    """Parse a whitespace/comma-separated list of lengths.

    ``None`` is returned when the attribute is absent.  Invalid items are
    kept as ``None``: only the affected character ignores the value.

    """
    value = node.get(attribute)
    if value is None:
        return None
    items = []
    for item in normalize(value).strip().split(' '):
        if not item:
            continue
        if LENGTH_RE.match(item) is None:
            items.append(None)
            continue
        try:
            items.append(size(surface, item, reference))
        except (TypeError, ValueError):
            items.append(None)
    return items


def parse_rotation_list(node):
    """Parse the ``rotate`` attribute; invalid values become ``None``."""
    value = node.get('rotate')
    if value is None:
        return None
    rotations = []
    for item in normalize(value).strip().split(' '):
        if not item:
            continue
        try:
            rotations.append(float(item))
        except ValueError:
            rotations.append(None)
    return rotations


def select_font(surface, node, font_size):
    """Select the font of ``node`` on the surface context.

    Measuring and painting both go through this function, so metrics come
    from the exact family, style, weight and size that will be drawn.

    """
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
    surface.context.set_font_size(font_size)


def resolve_font_size(surface, parent_font_size, node):
    """Resolve ``font-size`` of ``node`` against its parent's size."""
    old_font_size = surface.font_size
    surface.font_size = parent_font_size
    try:
        return size(surface, node.get('font-size', '12pt'))
    finally:
        surface.font_size = old_font_size


def baseline_alignment(node, ascent, descent, height, y_bearing,
                       max_x_advance, max_y_advance):
    """Return the vertical alignment offset used for ``node``.

    This mirrors the historical CairoSVG behaviour: only Western horizontal
    fonts are handled, through ``display-anchor`` and the simpler values of
    ``dominant-baseline``/``alignment-baseline``.

    """
    if not (max_x_advance > 0 and max_y_advance == 0):
        return 0
    display_anchor = node.get('display-anchor')
    alignment_baseline = (
        node.get('dominant-baseline') or node.get('alignment-baseline'))
    if display_anchor == 'middle':
        return -height / 2 - y_bearing
    if display_anchor == 'top':
        return -y_bearing
    if display_anchor == 'bottom':
        return -height - y_bearing
    if alignment_baseline in ('central', 'middle'):
        return (ascent + descent) / 2 - descent
    if alignment_baseline in (
            'text-before-edge', 'before_edge', 'top', 'hanging', 'text-top'):
        return ascent
    if alignment_baseline in (
            'text-after-edge', 'after_edge', 'bottom', 'text-bottom'):
        return -descent
    return 0


class Character:
    """One character of a laid-out text run."""

    __slots__ = (
        'node', 'char', 'x', 'y', 'advance', 'spacing', 'extents', 'rotate',
        'scale', 'target_x', 'target_y')

    def __init__(self, node, char, x, y, advance, spacing, extents, rotate):
        self.node = node
        self.char = char
        self.x = x
        self.y = y
        self.advance = advance
        # Letter spacing applied after this character (CSS semantics).
        self.spacing = spacing
        self.extents = extents
        self.rotate = rotate
        self.scale = 1.0
        # Absolute position requested by an explicit x/y value, if any.
        self.target_x = None
        self.target_y = None

    @property
    def end(self):
        """Return the end of the glyph advance."""
        return self.x + self.advance * self.scale


class NodeLayout:
    """Layout information for one ``text``/``tspan`` node."""

    __slots__ = (
        'node', 'chars', 'children', 'text_length', 'adjust', 'y_align',
        'font_size', 'lists', 'start', 'count', 'letter_spacing')

    def __init__(self, node, start, font_size):
        self.node = node
        self.chars = []
        self.children = []
        self.text_length = None
        self.adjust = 'spacing'
        self.y_align = 0
        self.font_size = font_size
        self.lists = {}
        self.start = start
        self.count = 0
        self.letter_spacing = 0

    def entries(self):
        """Return this node's own entries, then its descendants' entries."""
        result = list(self.chars)
        for child in self.children:
            result.extend(child.entries())
        return result


class _State:
    """Mutable cursor state while collecting a text run."""

    __slots__ = ('pen_x', 'pen_y', 'index', 'spacing')

    def __init__(self, x=0, y=0):
        self.pen_x = x
        self.pen_y = y
        self.index = 0
        # Letter spacing to insert before the next character.
        self.spacing = 0


def _iter_descendants(node):
    for child in node.children:
        yield child
        yield from _iter_descendants(child)


def _subtree(node):
    return [node] + list(_iter_descendants(node))


def subtree_has_text_length(surface, node):
    """Return whether ``node`` or a descendant has a valid textLength."""
    return any(
        parse_text_length(surface, child) is not None
        for child in _subtree(node))


def subtree_has_text_path(node):
    """Return whether ``node`` or a descendant uses ``textPath``."""
    return any(child.tag == 'textPath' for child in _subtree(node))


def subtree_has_letter_spacing(surface, node):
    """Return whether a multi-node run asks for letter spacing."""
    nodes = [
        child for child in _subtree(node) if child.tag in TEXT_TAGS]
    if len(nodes) < 2:
        return False
    return any(
        size(surface, child.get('letter-spacing')) for child in nodes)


def needs_layout(surface, node):
    """Return whether a full layout pass is needed for this text run."""
    if not is_text_root(node):
        return False
    if subtree_has_text_path(node):
        return False
    return (
        subtree_has_text_length(surface, node) or
        subtree_has_letter_spacing(surface, node))


# Attributes used to cache one node's share of a layout on the node dict
# itself.  Keeping the cache on the node (instead of in a surface-wide
# mapping keyed by ``id()``) avoids stale hits if a node is garbage
# collected and a new object later reuses its address.
LAYOUT_KEY = '_cairosvg_text_layout'
FONT_SIZE_KEY = '_cairosvg_text_font_size'


def build_layout(surface, root):
    """Lay out ``root``'s whole text subtree and cache it on the nodes.

    Every node of the run gets its own character list; the returned mapping
    from ``id(node)`` to that list is only a convenience for callers.

    """
    state = _State(*surface.cursor_position[:2])
    root_layout = _collect(surface, root, state, surface.font_size, [])
    ordered = root_layout.entries()
    _calibrate(root_layout, ordered)
    _apply_explicit_positions(ordered)
    _apply_anchors(root_layout, ordered)
    layouts = {}
    _store(root_layout, layouts)
    return layouts


def _store(layout, layouts):
    layouts[id(layout.node)] = layout.chars
    layout.node[LAYOUT_KEY] = layout.chars
    layout.node[FONT_SIZE_KEY] = layout.font_size
    for child in layout.children:
        _store(child, layouts)


def _collect(surface, node, state, font_size, chains):
    """Recursively collect characters, positions, advances and metrics."""
    layout = NodeLayout(node, state.index, font_size)
    layout.text_length = parse_text_length(surface, node)
    layout.adjust = length_adjust(node)
    layout.letter_spacing = _node_length(
        surface, node, 'letter-spacing', font_size)
    layout.lists = {
        'x': parse_length_list(surface, node, 'x', 'x'),
        'y': parse_length_list(surface, node, 'y', 'y'),
        'dx': parse_length_list(surface, node, 'dx', 'x'),
        'dy': parse_length_list(surface, node, 'dy', 'y'),
        'rotate': parse_rotation_list(node),
    }
    select_font(surface, node, font_size)
    ascent, descent, _, max_x_advance, max_y_advance = (
        surface.context.font_extents())

    chain = chains + [layout]
    text = node.text or ''
    for char in text:
        index = state.index
        extents = surface.context.text_extents(char)
        dx = _resolve(chain, 'dx', index, 0) or 0
        dy = _resolve(chain, 'dy', index, 0) or 0
        # The pen already includes every previous shift, so the current dx/dy
        # is added once, here.
        x = state.pen_x + state.spacing + dx
        y = state.pen_y + dy
        character = Character(
            node, char, x, y, extents[4], layout.letter_spacing, extents,
            _resolve_rotation(chain, index))
        target_x = _resolve(chain, 'x', index)
        target_y = _resolve(chain, 'y', index)
        if target_x is not None:
            # x is the absolute position, but dx shifts it too.
            character.target_x = target_x + dx
        if target_y is not None:
            character.target_y = target_y + dy
        layout.chars.append(character)
        state.pen_x = x + extents[4]
        state.pen_y = y
        state.spacing = layout.letter_spacing
        state.index += 1

    if layout.chars:
        text_extents = surface.context.text_extents(text)
        layout.y_align = baseline_alignment(
            node, ascent, descent, text_extents[3], text_extents[1],
            max_x_advance, max_y_advance)
        for character in layout.chars:
            character.y += layout.y_align
            if character.target_y is not None:
                character.target_y += layout.y_align
    else:
        # Keep the historical cursor behaviour for empty nodes: a first
        # x/y/dx/dy value still moves the following text position.
        values = layout.lists
        if values['x']:
            state.pen_x = values['x'][0] or 0
            state.spacing = 0
        if values['y']:
            state.pen_y = values['y'][0] or 0
        if values['dx']:
            state.pen_x += values['dx'][0] or 0
        if values['dy']:
            state.pen_y += values['dy'][0] or 0

    for child in node.children:
        if child.tag in TEXT_TAGS:
            child_font_size = resolve_font_size(surface, font_size, child)
            layout.children.append(
                _collect(surface, child, state, child_font_size, chain))
    layout.count = state.index - layout.start
    return layout


def _resolve(chain, name, index, fallback=None):
    """Resolve a positioning value from the nearest applicable node.

    ``chain`` is ordered from the root down to the character's node.  A
    descendant overrides an ancestor, but an ancestor's value still applies
    to characters that the descendant's shorter list does not cover, as
    described by the SVG 2 positioning algorithm.

    """
    for layout in reversed(chain):
        values = layout.lists.get(name)
        if values is None:
            continue
        offset = index - layout.start
        if 0 <= offset < len(values) and values[offset] is not None:
            return values[offset]
    return fallback


def _resolve_rotation(chain, index):
    """Resolve a rotation, falling back to the last provided value."""
    for layout in reversed(chain):
        values = layout.lists.get('rotate')
        if not values:
            continue
        offset = index - layout.start
        if 0 <= offset < len(values) and values[offset] is not None:
            return values[offset]
        if values[-1] is not None:
            return values[-1]
    return 0


def _node_length(surface, node, attribute, font_size):
    old_font_size = surface.font_size
    surface.font_size = font_size
    try:
        return size(surface, node.get(attribute))
    finally:
        surface.font_size = old_font_size


def advance_box(entries):
    """Return ``(start, end)`` of the advance box of ``entries``.

    ``letter-spacing`` counts after every character in CSS (including the
    last), and SVG 1.1 defines ``textLength`` as the sum of the advances,
    including the effect of ``letter-spacing``.  The box therefore ends with
    the trailing spacing of the last character, which is also what browsers
    report through ``getComputedTextLength``.

    """
    last = max(entries, key=lambda entry: entry.x)
    return (
        min(entry.x for entry in entries),
        max(entry.end for entry in entries) + last.spacing * last.scale)


def _items(layout, is_root=True):
    """Return calibration items, collapsing resolved descendant nodes.

    A descendant with its own ``textLength`` has already been calibrated
    and is treated as a single indivisible typographic character.

    """
    items = []
    if not is_root and layout.text_length is not None:
        entries = layout.entries()
        if entries:
            items.append((entries, True))
        return items
    for character in layout.chars:
        items.append(([character], False))
    for child in layout.children:
        items.extend(_items(child, is_root=False))
    return items


def _calibrate(layout, ordered):
    """Calibrate ``textLength``; descendants are adjusted first."""
    for child in layout.children:
        _calibrate(child, ordered)
    if layout.text_length is None:
        return
    items = _items(layout)
    if not items:
        return
    start, end = advance_box(layout.entries())
    natural = end - start
    if natural <= 0:
        # Zero-length runs (and zero-width textLength) cannot be calibrated
        # by scaling: there is no width to distribute.
        return
    if layout.adjust == 'spacingAndGlyphs':
        _adjust_glyphs(items, start, natural, layout.text_length)
    else:
        _adjust_spacing(items, layout.text_length - natural)
    # Calibration changed the node's width: content after it must follow the
    # calibrated end instead of sticking to the old natural position.
    shift = advance_box(layout.entries())[1] - end
    if shift:
        for entry in ordered[layout.start + layout.count:]:
            entry.x += shift


def _adjust_spacing(items, delta):
    """Distribute ``delta`` over the inter-character gaps only.

    There are ``n - 1`` gaps between ``n`` items.  A resolved descendant is
    one item, so its whole block moves rigidly by the same amount as a
    single character would; this keeps the block's internal, already
    calibrated spacing intact.

    """
    gaps = len(items) - 1
    if gaps <= 0:
        # A single item has no inter-character gap: 'spacing' cannot change
        # the rendered length.  This is the standard behaviour.
        return
    step = delta / gaps
    shift = 0
    for entries, resolved in items:
        for entry in entries:
            entry.x += shift
        shift += step if resolved else step * len(entries)


def _adjust_glyphs(items, start, natural, target):
    """Stretch or compress glyphs and advances from the first character.

    ``spacingAndGlyphs`` scales every glyph and every advance of the run,
    so the advance box becomes exactly ``textLength``.

    """
    scale = target / natural
    for entries, _ in items:
        for entry in entries:
            entry.x = start + (entry.x - start) * scale
            entry.scale *= scale


def _apply_explicit_positions(ordered):
    """Pin characters with explicit ``x``/``y`` and shift what follows.

    This is the SVG 2 "Adjust positions: x, y" step.  It runs after the
    ``textLength`` calibration, so a pinned character keeps the requested
    absolute coordinate and all following characters move with it.

    """
    shift_x = shift_y = 0
    for entry in ordered:
        if entry.target_x is not None:
            shift_x = entry.target_x - entry.x
        if entry.target_y is not None:
            shift_y = entry.target_y - entry.y
        if shift_x:
            entry.x += shift_x
        if shift_y:
            entry.y += shift_y


def _apply_anchors(layout, ordered):
    """Apply ``text-anchor`` once per anchored chunk.

    A character with an explicit ``x`` starts a new chunk.  The anchor of
    the node that starts the chunk applies to the whole chunk, and the
    chunk edges are the calibrated ones.

    """
    if not ordered:
        return
    chunks = []
    current = []
    for entry in ordered:
        if entry.target_x is not None and current:
            chunks.append(current)
            current = []
        current.append(entry)
    if current:
        chunks.append(current)
    for chunk in chunks:
        start, end = advance_box(chunk)
        anchor = chunk[0].node.get('text-anchor')
        if anchor == 'middle':
            shift = -(end - start) / 2
        elif anchor == 'end':
            shift = -(end - start)
        else:
            continue
        for entry in chunk:
            entry.x += shift


def cached_layout(surface, node):
    """Return the cached character list for ``node``, if any."""
    return node.get(LAYOUT_KEY)


def cached_font_size(surface, node):
    """Return the font size used when ``node`` was laid out."""
    return node.get(FONT_SIZE_KEY)
