"""
Group/element opacity compositing test suite.

These tests render SVG documents made of crossing color blocks and check
the actual PNG pixels of overlapping and non-overlapping regions. They
distinguish the two opacity semantics:

- ``opacity`` on a group (or ``use``) applies to the group as a whole:
  children are composited together first, the opacity is applied once to
  the result, so overlapping children do not get darker.
- ``opacity`` on a single element applies to its own fill/stroke, so
  overlapping elements (or fill/stroke of one element) do get darker
  where they overlap.

They also cover nested groups, clipping at the group boundary, masks and
filters combined with opacity (the opacity must not be multiplied a
second time), empty/fully transparent groups (no residue on siblings),
shared definitions (no leakage between siblings) and repeated export.

"""

import io

import pytest
from PIL import Image, ImageDraw

import cairosvg
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface

RED = 'rgb(255,0,0)'
BLUE = 'rgb(0,0,255)'
GREEN = 'rgb(0,255,0)'
WHITE = (255, 255, 255)

# Geometry of the crossing blocks used by most documents below:
# a red rectangle on [0,100[ and a blue one on [50,150[, both 50 high.
RECTS = ((0, 0, 100, 50, RED), (50, 0, 100, 50, BLUE))
RED_ONLY = (25, 25)      # inside the red rectangle only
OVERLAP = (75, 25)       # covered by both rectangles
BLUE_ONLY = (125, 25)    # inside the blue rectangle only


def render(svg, **kwargs):
    """Render ``svg`` on a white background, return a PIL RGB image."""
    png = cairosvg.svg2png(
        bytestring=svg.encode(), background_color='white', **kwargs)
    return Image.open(io.BytesIO(png)).convert('RGB')


def assert_pixel(image, xy, expected, tolerance=2):
    """Check one pixel, allowing small rounding differences."""
    got = image.getpixel(xy)
    assert all(abs(g - e) <= tolerance for g, e in zip(got, expected)), (
        f'pixel {xy}: got {got}, expected {expected}')


def blend(source, destination):
    """Alpha-composite ``source`` (RGBA, 0-255 alpha) over ``destination``."""
    sr, sg, sb, sa = source
    dr, dg, db = destination
    return tuple(round(s * sa / 255 + d * (1 - sa / 255))
                 for s, d in ((sr, dr), (sg, dg), (sb, db)))


# Expected colors, computed from the compositing semantics (255*0.5=127.5
# is rounded to 127 by cairo, a tolerance of 2 covers the difference).
GROUP_RED = blend((255, 0, 0, 127), WHITE)      # (255, 128, 128)
GROUP_OVERLAP = blend((0, 0, 255, 127), WHITE)  # (127, 128, 255)
GROUP_BLUE = blend((0, 0, 255, 127), WHITE)     # (127, 128, 255)
CHILD_RED = blend((255, 0, 0, 127), WHITE)      # (255, 128, 128)
CHILD_OVERLAP = blend((0, 0, 255, 127), CHILD_RED)   # (128, 128, 191)
CHILD_BLUE = blend((0, 0, 255, 127), WHITE)     # (127, 128, 255)

TWO_BLOCKS = (
    f'<rect x="0" y="0" width="100" height="50" fill="{RED}"/>\n'
    f'<rect x="50" y="0" width="100" height="50" fill="{BLUE}"/>')
SVG_OPEN = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="150" height="50">')
SVG_CLOSE = '</svg>'


def test_group_opacity_applies_once_to_whole_group():
    """Opacity on a group: overlap is NOT darker than single coverage."""
    image = render(f'{SVG_OPEN}<g opacity="0.5">{TWO_BLOCKS}</g>{SVG_CLOSE}')
    assert_pixel(image, RED_ONLY, GROUP_RED)
    assert_pixel(image, OVERLAP, GROUP_OVERLAP)
    assert_pixel(image, BLUE_ONLY, GROUP_BLUE)


def test_child_opacity_applies_per_element():
    """Same opacity on each child: the overlap IS darker (other semantics)."""
    image = render(
        f'{SVG_OPEN}'
        f'<rect x="0" y="0" width="100" height="50" fill="{RED}" '
        f'opacity="0.5"/>'
        f'<rect x="50" y="0" width="100" height="50" fill="{BLUE}" '
        f'opacity="0.5"/>'
        f'{SVG_CLOSE}')
    assert_pixel(image, RED_ONLY, CHILD_RED)
    assert_pixel(image, OVERLAP, CHILD_OVERLAP)
    assert_pixel(image, BLUE_ONLY, CHILD_BLUE)


def test_group_and_child_semantics_differ_in_overlap_only():
    """The two documents must differ exactly in the overlap region."""
    group = render(f'{SVG_OPEN}<g opacity="0.5">{TWO_BLOCKS}</g>{SVG_CLOSE}')
    child = render(
        f'{SVG_OPEN}'
        f'<rect x="0" y="0" width="100" height="50" fill="{RED}" '
        f'opacity="0.5"/>'
        f'<rect x="50" y="0" width="100" height="50" fill="{BLUE}" '
        f'opacity="0.5"/>'
        f'{SVG_CLOSE}')
    assert group.getpixel(RED_ONLY) == child.getpixel(RED_ONLY)
    assert group.getpixel(BLUE_ONLY) == child.getpixel(BLUE_ONLY)
    assert group.getpixel(OVERLAP) != child.getpixel(OVERLAP)


def test_nested_groups_multiply_opacity_once_per_group():
    """0.5 inside 0.5 gives 0.25 uniformly, overlaps included."""
    image = render(
        f'{SVG_OPEN}<g opacity="0.5"><g opacity="0.5">{TWO_BLOCKS}</g></g>'
        f'{SVG_CLOSE}')
    expected_red = blend((255, 0, 0, 63), WHITE)
    expected_blue = blend((0, 0, 255, 63), WHITE)
    assert_pixel(image, RED_ONLY, expected_red)
    assert_pixel(image, OVERLAP, expected_blue)
    assert_pixel(image, BLUE_ONLY, expected_blue)


def test_group_opacity_with_boundary_clip():
    """A clip-path cutting the group: faded inside, untouched outside."""
    image = render(
        f'{SVG_OPEN}'
        f'<defs><clipPath id="c">'
        f'<rect x="25" y="0" width="100" height="50"/></clipPath></defs>'
        f'<g clip-path="url(#c)" opacity="0.5">{TWO_BLOCKS}</g>'
        f'{SVG_CLOSE}')
    # Outside the clip region the document stays white...
    assert_pixel(image, (10, 25), WHITE, tolerance=0)
    assert_pixel(image, (140, 25), WHITE, tolerance=0)
    # ...inside, the group is composited as a whole: uniform fade.
    assert_pixel(image, (40, 25), GROUP_RED)
    assert_pixel(image, OVERLAP, GROUP_OVERLAP)
    assert_pixel(image, (110, 25), GROUP_BLUE)


def test_mask_and_opacity_are_not_multiplied_twice():
    """mask + opacity on one element: opacity applied once, not twice."""
    image = render(
        f'{SVG_OPEN}'
        f'<defs><mask id="m">'
        f'<rect x="0" y="0" width="150" height="50" fill="white"/>'
        f'</mask></defs>'
        f'<rect x="0" y="0" width="150" height="50" fill="{RED}" '
        f'mask="url(#m)" opacity="0.5"/>'
        f'{SVG_CLOSE}')
    # 0.5 would be (255,128,128); a double application gives (255,191,191).
    assert_pixel(image, (75, 25), GROUP_RED)


def test_group_mask_clip_and_opacity_compose_together():
    """Nested group + clip + mask + opacity: still a single fade."""
    image = render(
        f'{SVG_OPEN}'
        f'<defs>'
        f'<mask id="m"><rect x="0" y="0" width="150" height="50" '
        f'fill="white"/></mask>'
        f'<clipPath id="c"><rect x="0" y="0" width="150" height="50"/>'
        f'</clipPath></defs>'
        f'<g opacity="0.5">'
        f'<g mask="url(#m)" clip-path="url(#c)" opacity="0.5">'
        f'{TWO_BLOCKS}</g></g>'
        f'{SVG_CLOSE}')
    expected_red = blend((255, 0, 0, 63), WHITE)
    expected_blue = blend((0, 0, 255, 63), WHITE)
    assert_pixel(image, RED_ONLY, expected_red)
    assert_pixel(image, OVERLAP, expected_blue)
    assert_pixel(image, BLUE_ONLY, expected_blue)


def test_filter_and_opacity_are_not_multiplied_twice():
    """filter + opacity on one element: opacity applied once."""
    image = render(
        f'{SVG_OPEN}'
        f'<defs><filter id="f"><feBlend mode="normal"/></filter></defs>'
        f'<rect x="0" y="0" width="150" height="50" fill="{RED}" '
        f'filter="url(#f)" opacity="0.5"/>'
        f'{SVG_CLOSE}')
    assert_pixel(image, (75, 25), GROUP_RED)


def test_use_opacity_composites_referenced_content_as_a_group():
    """opacity on <use> applies to the referenced content as a whole."""
    image = render(
        f'{SVG_OPEN}'
        f'<defs><g id="icon">{TWO_BLOCKS}</g></defs>'
        f'<use href="#icon" opacity="0.5"/>'
        f'{SVG_CLOSE}')
    assert_pixel(image, RED_ONLY, GROUP_RED)
    assert_pixel(image, OVERLAP, GROUP_OVERLAP)
    assert_pixel(image, BLUE_ONLY, GROUP_BLUE)


def test_empty_and_transparent_groups_leave_no_residue():
    """Empty or fully transparent groups must not affect what follows."""
    baseline = render(
        f'{SVG_OPEN}'
        f'<rect x="0" y="0" width="50" height="50" fill="{GREEN}"/>'
        f'<rect x="100" y="0" width="50" height="50" fill="{GREEN}"/>'
        f'{SVG_CLOSE}')
    for prefix in (
            # Empty group carrying transform, clip and opacity.
            '<g transform="translate(5,5) scale(2)" opacity="0.5" '
            '  clip-path="url(#c)"></g>',
            # Fully transparent group with visible content.
            f'<g opacity="0"><rect x="0" y="0" width="150" height="50" '
            f'fill="{RED}"/></g>',
            # Empty group with a mask (a group canvas is still pushed).
            '<g mask="url(#m)"></g>'):
        image = render(
            f'{SVG_OPEN}'
            f'<defs>'
            f'<clipPath id="c"><rect width="4" height="4"/></clipPath>'
            f'<mask id="m"><rect width="10" height="10" fill="white"/>'
            f'</mask></defs>'
            f'{prefix}'
            f'<rect x="0" y="0" width="50" height="50" fill="{GREEN}"/>'
            f'<rect x="100" y="0" width="50" height="50" fill="{GREEN}"/>'
            f'{SVG_CLOSE}')
        assert list(image.getdata()) == list(baseline.getdata())


def test_shared_pattern_opacity_does_not_leak_to_siblings():
    """fill-opacity of one sibling must not stick to the shared pattern."""
    image = render(
        f'{SVG_OPEN}'
        f'<defs><pattern id="p" width="10" height="10" '
        f'patternUnits="userSpaceOnUse">'
        f'<rect width="10" height="10" fill="{BLUE}"/></pattern></defs>'
        f'<rect x="0" y="0" width="50" height="50" fill="url(#p)" '
        f'fill-opacity="0.5"/>'
        f'<rect x="50" y="0" width="50" height="50" fill="url(#p)"/>'
        f'<rect x="100" y="0" width="50" height="50" fill="url(#p)"/>'
        f'{SVG_CLOSE}')
    assert_pixel(image, (25, 25), GROUP_BLUE)
    assert_pixel(image, (75, 25), (0, 0, 255), tolerance=0)
    assert_pixel(image, (125, 25), (0, 0, 255), tolerance=0)


def test_shared_pattern_survives_second_use():
    """An objectBoundingBox pattern must give the same tiles twice.

    The first use must not consume the shared definition's width/height:
  the second rectangle must show the same pattern as the first.
    """
    image = render(
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
        '<defs><pattern id="p" width="0.5" height="0.5">'
        f'<rect width="4" height="4" fill="{BLUE}"/></pattern></defs>'
        '<rect x="0" y="0" width="20" height="20" fill="url(#p)"/>'
        '<rect x="20" y="0" width="20" height="20" fill="url(#p)"/>'
        '</svg>')
    left = [image.getpixel((x, y)) for y in range(20) for x in range(20)]
    right = [image.getpixel((x, y)) for y in range(20) for x in range(20, 40)]
    assert left == right
    assert left[0] == (0, 0, 255)


def test_shared_definitions_are_not_mutated_by_rendering():
    """Rendering must leave shared mask/pattern definitions untouched."""
    doc = (
        f'{SVG_OPEN}'
        f'<defs>'
        f'<mask id="m"><rect x="0" y="0" width="150" height="50" '
        f'fill="white"/></mask>'
        f'<pattern id="p" width="10" height="10" '
        f'patternUnits="userSpaceOnUse">'
        f'<rect width="10" height="10" fill="{BLUE}"/></pattern>'
        f'</defs>'
        f'<g mask="url(#m)" opacity="0.5">{TWO_BLOCKS}</g>'
        f'<rect x="0" y="0" width="50" height="50" fill="url(#p)" '
        f'fill-opacity="0.5"/>'
        f'{SVG_CLOSE}')
    tree = Tree(bytestring=doc.encode())
    output = io.BytesIO()
    surface = PNGSurface(tree, output, 96)
    surface.finish()
    mask_node = surface.masks['m']
    assert mask_node.tag == 'mask'
    assert mask_node.get('opacity') is None
    pattern_node = surface.patterns['p']
    assert pattern_node.tag == 'pattern'
    assert pattern_node.get('opacity') is None
    assert pattern_node.get('width') == '10'
    assert pattern_node.get('height') == '10'


FULL_DOCUMENT = (
    f'{SVG_OPEN}'
    f'<defs>'
    f'<mask id="m"><rect x="0" y="0" width="150" height="50" fill="white"/>'
    f'</mask>'
    f'<clipPath id="c"><rect x="0" y="0" width="150" height="50"/>'
    f'</clipPath>'
    f'<pattern id="p" width="10" height="10" patternUnits="userSpaceOnUse">'
    f'<rect width="10" height="10" fill="{BLUE}"/></pattern>'
    f'<g id="icon">{TWO_BLOCKS}</g>'
    f'</defs>'
    f'<g opacity="0.5"><g clip-path="url(#c)" opacity="0.5">'
    f'{TWO_BLOCKS}</g></g>'
    f'<rect x="0" y="0" width="50" height="50" fill="url(#p)" '
    f'fill-opacity="0.5"/>'
    f'<rect x="50" y="0" width="50" height="50" fill="url(#p)"/>'
    f'<g mask="url(#m)" opacity="0.5">'
    f'<rect x="100" y="0" width="50" height="50" fill="{RED}"/></g>'
    f'<use href="#icon" opacity="0.3"/>'
    f'<g opacity="0"><rect x="0" y="0" width="150" height="50" '
    f'fill="{GREEN}"/></g>'
    f'{SVG_CLOSE}')


def test_repeated_export_is_deterministic():
    """Exporting the same document repeatedly gives identical bytes."""
    renders = [cairosvg.svg2png(bytestring=FULL_DOCUMENT.encode())
               for _ in range(3)]
    assert renders[0] == renders[1] == renders[2]


def test_no_opacity_output_is_unchanged():
    """Compatibility: documents without opacity render exact colors."""
    image = render(f'{SVG_OPEN}{TWO_BLOCKS}{SVG_CLOSE}')
    assert_pixel(image, RED_ONLY, (255, 0, 0), tolerance=0)
    assert_pixel(image, OVERLAP, (0, 0, 255), tolerance=0)
    assert_pixel(image, BLUE_ONLY, (0, 0, 255), tolerance=0)


def test_single_element_opacity_keeps_paint_semantics():
    """Compatibility: opacity on one element folds into fill and stroke.

    The stroke of a single semi-transparent element darkens where it
    overlaps the fill (per-paint semantics), unlike a group fade.
    """
    image = render(
        f'{SVG_OPEN}'
        f'<rect x="25" y="5" width="100" height="40" fill="{RED}" '
        f'stroke="{BLUE}" stroke-width="10" opacity="0.5"/>'
        f'{SVG_CLOSE}')
    # Fill only: red at 0.5 over white.
    assert_pixel(image, (75, 25), CHILD_RED)
    # Stroke over fill: blue at 0.5 over the faded red.
    assert_pixel(image, (27, 25), CHILD_OVERLAP)


# ---------------------------------------------------------------------------
# Visual comparison sheet
# ---------------------------------------------------------------------------

def _draw_rects(size, rects, opacity=None):
    """Draw solid rectangles on a transparent layer."""
    layer = Image.new('RGBA', size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    for x, y, width, height, color in rects:
        rgb = tuple(int(color[4:-1].split(',')[i]) for i in range(3))
        draw.rectangle([x, y, x + width - 1, y + height - 1],
                       fill=rgb + (255,))
    if opacity is not None:
        alpha = layer.getchannel('A').point(lambda a: int(a * opacity))
        layer.putalpha(alpha)
    return layer


def _over_white(size, layer):
    base = Image.new('RGBA', size, (255, 255, 255, 255))
    base.alpha_composite(layer)
    return base.convert('RGB')


def reference_group(size, rects, opacity):
    """Reference for group semantics: fade the composited layer once."""
    return _over_white(size, _draw_rects(size, rects, opacity))


def reference_children(size, rects, opacity):
    """Reference for per-element semantics: fade each element in turn."""
    base = Image.new('RGBA', size, (255, 255, 255, 255))
    for rect in rects:
        base.alpha_composite(_draw_rects(size, [rect], opacity))
    return base.convert('RGB')


SCENARIOS = (
    ('group opacity 0.5',
     lambda: render(f'{SVG_OPEN}<g opacity="0.5">{TWO_BLOCKS}</g>'
                    f'{SVG_CLOSE}'),
     lambda: reference_group((150, 50), RECTS, 0.5)),
    ('child opacity 0.5',
     lambda: render(
         f'{SVG_OPEN}'
         f'<rect x="0" y="0" width="100" height="50" fill="{RED}" '
         f'opacity="0.5"/>'
         f'<rect x="50" y="0" width="100" height="50" fill="{BLUE}" '
         f'opacity="0.5"/>'
         f'{SVG_CLOSE}'),
     lambda: reference_children((150, 50), RECTS, 0.5)),
    ('nested 0.5 x 0.5',
     lambda: render(
         f'{SVG_OPEN}<g opacity="0.5"><g opacity="0.5">{TWO_BLOCKS}</g>'
         f'</g>{SVG_CLOSE}'),
     lambda: reference_group(
         (150, 50), RECTS, 0.25)),
    ('mask + opacity 0.5',
     lambda: render(
         f'{SVG_OPEN}<defs><mask id="m">'
         f'<rect x="0" y="0" width="150" height="50" fill="white"/></mask>'
         f'</defs><g mask="url(#m)" opacity="0.5">{TWO_BLOCKS}</g>'
         f'{SVG_CLOSE}'),
     lambda: reference_group((150, 50), RECTS, 0.5)),
    ('clip boundary + opacity',
     lambda: render(
         f'{SVG_OPEN}<defs><clipPath id="c">'
         f'<rect x="25" y="0" width="100" height="50"/></clipPath></defs>'
         f'<g clip-path="url(#c)" opacity="0.5">{TWO_BLOCKS}</g>'
         f'{SVG_CLOSE}'),
     # Group fade, then everything outside the clip box erased.
     lambda: _over_white(
         (150, 50),
         Image.composite(
             _draw_rects((150, 50), RECTS, 0.5),
             Image.new('RGBA', (150, 50), (0, 0, 0, 0)),
             _clip_mask((150, 50), (25, 0, 124, 49))))),
    ('use opacity 0.5',
     lambda: render(
         f'{SVG_OPEN}<defs><g id="icon">{TWO_BLOCKS}</g></defs>'
         f'<use href="#icon" opacity="0.5"/>'
         f'{SVG_CLOSE}'),
     lambda: reference_group((150, 50), RECTS, 0.5)),
)


def _clip_mask(size, box):
    mask = Image.new('L', size, 0)
    ImageDraw.Draw(mask).rectangle(box, fill=255)
    return mask


def build_comparison_sheet(scale=3):
    """Build a side-by-side sheet: CairoSVG output vs reference compositing.

    Each row is one scenario; left column is CairoSVG's PNG output, right
    column is an independent PIL compositing of the expected semantics.
    """
    label_width, row_height = 230, 50 * scale + 10
    width = label_width + 2 * 150 * scale + 20
    height = (len(SCENARIOS) + 1) * row_height
    sheet = Image.new('RGB', (width, height), (245, 245, 245))
    draw = ImageDraw.Draw(sheet)
    draw.text((label_width + 40, 5), 'CairoSVG', fill=(0, 0, 0))
    draw.text((label_width + 150 * scale + 60, 5), 'Reference', fill=(0, 0, 0))
    for row, (name, render_actual, render_expected) in enumerate(SCENARIOS, 1):
        y = row * row_height + 5
        draw.text((5, y + row_height // 2 - 5), name, fill=(0, 0, 0))
        actual = render_actual().resize((150 * scale, 50 * scale),
                                        Image.NEAREST)
        expected = render_expected().resize((150 * scale, 50 * scale),
                                            Image.NEAREST)
        sheet.paste(actual, (label_width, y))
        sheet.paste(expected, (label_width + 150 * scale + 20, y))
    return sheet


def test_rendered_rows_match_reference_compositing():
    """Each CairoSVG row matches its independent reference within rounding."""
    for name, render_actual, render_expected in SCENARIOS:
        actual = render_actual()
        expected = render_expected()
        assert actual.size == expected.size, name
        differences = [
            abs(a - e)
            for a, e in zip(actual.getdata(), expected.getdata())
            for a, e in zip(a, e)]
        assert max(differences) <= 2, (
            f'{name}: max channel difference {max(differences)}')


if __name__ == '__main__':
    build_comparison_sheet().save('group-compositing-comparison.png')
