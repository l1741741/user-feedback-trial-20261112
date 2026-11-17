"""Regression tests for path geometry.

The tests render paths and check two things that must not change:

* the outline of a path is the same whatever the command spelling used
  (absolute, relative, smooth or not, explicit close or not);
* the stroked outline follows the SVG stroke properties on every kind of
  segment (lines, quadratic and cubic curves, closing segments).

They complement :mod:`cairosvg.test_stroke_joins`, which focuses on joins,
caps and invalid values.

"""

import io

import pytest
from PIL import Image, ImageChops

from . import svg2png

SCALE = 4
SIZE = 100


def render(path_data, stroke='black', width=4, join='miter', cap='butt',
           fill='none', extra=''):
    """Render one path and return the RGBA image."""
    source = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}"
        height="{SIZE}" viewBox="0 0 {SIZE} {SIZE}">
      <path d="{path_data}" fill="{fill}" stroke="{stroke}"
            stroke-width="{width}" stroke-linejoin="{join}"
            stroke-linecap="{cap}" {extra}/>
    </svg>'''
    png = svg2png(
        bytestring=source.encode(), output_width=SIZE * SCALE,
        output_height=SIZE * SCALE)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def same_image(first, second):
    """Compare two renderings exactly."""
    return ImageChops.difference(first, second).getbbox() is None


def ink_bounding_box(image, threshold=8):
    """Bounding box of the painted pixels, in user units."""
    mask = image.getchannel('A').point(
        lambda value: 255 if value > threshold else 0)
    box = mask.getbbox()
    if box is None:
        return None
    return tuple(round(position / SCALE, 2) for position in box)


@pytest.mark.parametrize('absolute,relative', [
    ('M10 50 L50 10 L90 50', 'M10 50 l40 -40 l40 40'),
    ('M10 50 L50 10 L90 50 L90 90', 'M10 50 l40 -40 l40 40 l0 40'),
    ('M10 90 C20 10 80 10 90 90', 'M10 90 c10 -80 70 -80 80 0'),
    ('M10 90 C20 10 40 10 50 90 C60 10 80 10 90 90',
     'M10 90 c10 -80 30 -80 40 0 c10 -80 30 -80 40 0'),
    ('M10 90 Q50 10 90 90', 'M10 90 q40 -80 80 0'),
    ('M10 50 H50 V10 H90 V50', 'M10 50 h40 v-40 h40 v40'),
    ('M10 10 L90 10 L90 90 Z', 'M10 10 L90 10 L90 90 z'),
])
@pytest.mark.parametrize('join', ['miter', 'round', 'bevel'])
@pytest.mark.parametrize('cap', ['butt', 'round', 'square'])
def test_absolute_and_relative_commands_render_identically(
        absolute, relative, join, cap):
    """Relative commands draw exactly the same outline as absolute ones."""
    options = dict(join=join, cap=cap, width=6)
    assert same_image(
        render(absolute, **options), render(relative, **options))


def test_relative_quadratic_smooth_curve():
    """``t`` is the relative form of ``T`` and follows a ``Q``/``q`` curve."""
    absolute = render(
        'M10 90 Q50 10 90 90 T90 10 L90 40', width=6)
    relative = render(
        'M10 90 q40 -80 80 0 q40 80 0 -80 l0 30', width=6)
    assert same_image(absolute, relative)


def test_relative_cubic_smooth_curve():
    """``s`` is the relative form of ``S`` and follows a ``C``/``c`` curve."""
    absolute = render(
        'M10 90 C20 10 40 10 50 90 S90 10 90 90 L90 40', width=6)
    relative = render(
        'M10 90 c10 -80 30 -80 40 0 s40 -80 40 0 l0 -50', width=6)
    assert same_image(absolute, relative)


@pytest.mark.parametrize('join', ['miter', 'round', 'bevel'])
def test_close_and_explicit_line_to_start_are_equivalent(join):
    """Drawing back to the start before ``Z`` changes nothing.

    ``Z`` already draws that closing segment; adding it explicitly must not
    add a join or a cap, and must use the same join as the other corners.
    """
    closed = render('M20 80 L50 20 L80 80 Z', join=join, width=6,
                    extra='stroke-miterlimit="10"')
    explicit = render('M20 80 L50 20 L80 80 L20 80 Z', join=join, width=6,
                      extra='stroke-miterlimit="10"')
    assert same_image(closed, explicit)


@pytest.mark.parametrize('path_data', [
    'M10 80 L40 20 Q65 5 80 40 C95 70 55 95 25 75 Z',
    'M10 50 A30 20 0 0 1 70 50 L70 80',
    'M10 50 Q30 10 50 50 T90 50',
    'M10 50 C20 10 40 10 50 50 S80 90 90 50',
])
def test_paths_with_every_segment_kind_still_render(path_data):
    """Mixed straight/quadratic/cubic/arc/close paths keep rendering."""
    image = render(path_data, width=5)
    assert ink_bounding_box(image) is not None
    assert sum(image.getchannel('A').histogram()[9:]) > 500


def test_fill_and_stroke_are_independent():
    """The fill uses the path geometry; the stroke adds half the width."""
    filled = render('M30 30 L70 30 L70 70 L30 70 Z', fill='red', stroke='none')
    stroked = render(
        'M30 30 L70 30 L70 70 L30 70 Z', fill='red', stroke='black',
        width=10)
    assert ink_bounding_box(filled) == (30, 30, 70, 70)
    assert ink_bounding_box(stroked) == (25, 25, 75, 75)


@pytest.mark.parametrize('path_data', [
    'M50 50',                       # move only: no subpath, so nothing to draw
    'M50 50 Z',                      # one point, then close
    'M50 50 L50 50',                 # zero-length line
    'M50 50 c0 0 0 0 0 0',           # zero-length cubic
    'M50 50 q0 0 0 0',               # zero-length quadratic
    'M10 50 L90 50 M50 50 L50 50',   # real geometry and a single point
])
def test_degenerate_paths_keep_rendering(path_data):
    """Degenerate paths never raise, and keep the other subpaths visible."""
    image = render(path_data, width=8, cap='square')
    if path_data == 'M50 50':
        # A lone move-to does not start a subpath: there is no cap.
        assert ink_bounding_box(image) is None
    else:
        assert ink_bounding_box(image) is not None


def test_duplicated_points_do_not_move_the_outline():
    """Repeating a point does not change the stroked outline."""
    plain = render('M10 50 L50 10 L90 50', width=6, join='miter',
                   extra='stroke-miterlimit="10"')
    duplicated = render(
        'M10 50 L10 50 L50 10 L50 10 L90 50 L90 50', width=6, join='miter',
        extra='stroke-miterlimit="10"')
    assert ink_bounding_box(plain) == ink_bounding_box(duplicated)


@pytest.mark.parametrize('path_data', [
    'Z', 'z', 'Z M10 10', 'M10 10 Z', 'M10 10 L20 20 Z Z', 'M10 10 Z Z',
    'm10 10 z', 'M10 10 L20 20 L10 10 Z'])
def test_close_commands_without_subpath_are_ignored(path_data):
    """A close without a current subpath is ignored, not an error.

    SVG allows ``Z`` at the beginning of the data, or repeated after a subpath
    is already closed; such commands must not raise.
    """
    render(path_data, width=6, cap='square', extra='stroke-miterlimit="10"')


@pytest.mark.parametrize('path_data', [
    'M10 50 a20 20 0 6 0 40 0',     # large-arc flag out of range
    'M10 50 a20 20 0 1 7 40 0',     # sweep flag out of range
    'M10 50 a20 20 0 -1 0 40 0',    # negative sweep flag
    'M10 50 a20 20 0 1 -1 40 0',    # negative large-arc flag
])
def test_invalid_arc_flags_do_not_raise(path_data):
    """Only 0 and 1 are valid for arc flags; anything else is ignored.

    The flags are read one character at a time, so a negative number or a
    digit above 1 used to raise ``ValueError`` and abort the whole document.
    """
    render(path_data, width=4)
