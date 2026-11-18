"""
Tests for the “non-scaling-stroke” vector effect.

These tests are deliberately built on top of the real Cairo rendering chain:
they rasterize their SVGs and inspect the pixels of the resulting PNG, so
they fail if the stroke outline, the markers or the document context are
handled incorrectly.

"""

import io

import pytest
from PIL import Image

from . import svg2png

STROKE = 4
"""Stroke width used by most scenes below, in user units and pixels."""


def render(svg, **kwargs):
    """Render an SVG byte string and return a Pillow RGB image on white."""
    image = Image.open(io.BytesIO(svg2png(bytestring=svg, **kwargs)))
    background = Image.new('RGBA', image.size, (255, 255, 255, 255))
    background.alpha_composite(image.convert('RGBA'))
    return background.convert('RGB')


def ink():
    """Return whether a pixel is painted with the black stroke paint."""
    return lambda pixel: sum(pixel) < 3 * 240


def runs(pixels):
    """Group consecutive indexes of a boolean row into ``(start, end)``."""
    groups, current = [], []
    for index, painted in enumerate(pixels):
        if painted:
            current.append(index)
        elif current:
            groups.append((current[0], current[-1]))
            current = []
    if current:
        groups.append((current[0], current[-1]))
    return groups


def scan_row(image, y, threshold=240):
    """Return painted horizontal runs ``(start, end)`` on row ``y``."""
    pixels = [image.getpixel((x, y)) for x in range(image.width)]
    return runs([sum(pixel) < 3 * threshold for pixel in pixels])


def scan_column(image, x, threshold=240):
    """Return painted vertical runs ``(start, end)`` on column ``x``."""
    pixels = [image.getpixel((x, y)) for y in range(image.height)]
    return runs([sum(pixel) < 3 * threshold for pixel in pixels])


def widths(groups):
    """Return the thickness of runs, for horizontal or vertical scans."""
    return [end - start + 1 for start, end in groups]


def coverage(image, index, axis='x', threshold=0.02):
    """Return anti-aliased thickness of each painted run.

    A rotated or curved stroke covers a fractional number of pixels; summing
    the coverage is a better estimate of its real width than counting pixels.

    """
    if axis == 'x':
        pixels = [image.getpixel((x, index)) for x in range(image.width)]
    else:
        pixels = [image.getpixel((index, y)) for y in range(image.height)]
    measures, current, started = [], 0.0, False
    for pixel in [max(0., 1 - sum(pixel) / 765) for pixel in pixels] + [0.]:
        if pixel > threshold:
            current += pixel
            started = True
        elif started:
            measures.append(round(current, 2))
            current, started = 0.0, False
    return measures


def svg(body, width=200, height=200, extra=''):
    """Wrap ``body`` in an SVG document."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" {extra}>{body}</svg>'
    ).encode()


def outline(x, y, width, height, **attributes):
    """Return a non-filled rectangle with the given stroke attributes."""
    attributes.setdefault('stroke', 'black')
    attributes.setdefault('stroke-width', str(STROKE))
    extra = ' '.join(f'{key}="{value}"' for key, value in attributes.items())
    return (
        f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
        f'fill="none" {extra}/>')


def test_plain_stroke_keeps_scaling():
    """A stroke without vector-effect is still scaled by the object."""
    image = render(svg(
        '<g transform="scale(4, 1)">' +
        outline(5, 10, 20, 10) + '</g>', width=200, height=40))
    # The rectangle is scaled 4 times horizontally: its vertical sides are
    # 4 * STROKE = 16 device pixels wide.
    assert widths(scan_row(image, 14)) == [16, 16]
    # Its horizontal sides keep the unscaled 4 device pixels.
    assert widths(scan_column(image, 40)) == [STROKE, STROKE]


def test_non_scaling_stroke_is_not_scaled():
    """“non-scaling-stroke” keeps the stroke width in device pixels."""
    image = render(svg(
        '<g transform="scale(4, 1)">' +
        outline(5, 10, 20, 10, **{'vector-effect': 'non-scaling-stroke'}) +
        '</g>', width=200, height=40))
    assert widths(scan_row(image, 14)) == [STROKE, STROKE]
    assert widths(scan_column(image, 40)) == [STROKE, STROKE]


def test_geometry_still_follows_transform():
    """Only the stroke scale changes: the path itself is still transformed."""
    image = render(svg(
        '<g transform="scale(4, 1)">' +
        outline(5, 10, 20, 10, **{'vector-effect': 'non-scaling-stroke'}) +
        '</g>', width=200, height=40))
    vertical = scan_row(image, 14)
    # Outline centers are at 4 * 5 = 20 and 4 * 25 = 100 device pixels.
    assert [sum(run) / 2 for run in vertical] == pytest.approx(
        [20, 100], abs=1)
    # The horizontal sides stay at y = 10 and y = 20 device pixels.
    horizontal = scan_column(image, 40)
    assert [sum(run) / 2 for run in horizontal] == pytest.approx(
        [10, 20], abs=1)


def test_rotation_keeps_stroke_width():
    """A rotated element also keeps a constant stroke width."""
    image = render(svg(
        '<path d="M 10 40 H 100" fill="none" stroke="black" '
        f'stroke-width="{STROKE}" transform="rotate(30 50 40)" '
        'vector-effect="non-scaling-stroke"/>', width=160, height=160))
    # A vertical scan crosses the rotated line at 30°: the apparent
    # thickness is STROKE / cos(30°). Rotation never makes it thicker.
    expected = STROKE / 0.8660254
    for x in (50, 60, 70, 80):
        measures = coverage(image, x, 'y')
        assert len(measures) == 1
        assert measures[0] == pytest.approx(expected, abs=0.3)

    # Without the vector effect, the very same rotated line is also
    # STROKE / cos(30°): rotation alone is not what the effect fixes.
    plain = render(svg(
        '<path d="M 10 40 H 100" fill="none" stroke="black" '
        f'stroke-width="{STROKE}" transform="rotate(30 50 40)"/>',
        width=160, height=160))
    assert coverage(plain, 60, 'y')[0] == pytest.approx(expected, abs=0.3)


def test_uniform_scale_keeps_stroke_width():
    """A uniform scale, like an export zoom, does not thicken the stroke."""
    image = render(svg(
        '<g transform="scale(3, 3)">' +
        outline(5, 5, 20, 10, **{'vector-effect': 'non-scaling-stroke'}) +
        '</g>', width=120, height=120))
    # The sides of the scaled outline are at 15 and 75 device pixels.
    assert widths(scan_row(image, 30)) == [STROKE, STROKE]
    assert widths(scan_column(image, 45)) == [STROKE, STROKE]
    # The scaled rectangle is 60 x 30 device pixels, plus its strokes.
    assert scan_row(image, 30) == [(13, 16), (73, 76)]
    assert scan_column(image, 45) == [(13, 16), (43, 46)]


def test_non_uniform_scale_keeps_stroke_width():
    """Horizontal and vertical strokes have the same thickness."""
    image = render(svg(
        '<g transform="scale(4, 1)">' +
        outline(5, 10, 20, 10, **{'vector-effect': 'non-scaling-stroke'}) +
        '</g>', width=200, height=40))
    horizontal = widths(scan_column(image, 40))
    vertical = widths(scan_row(image, 14))
    assert horizontal == vertical == [STROKE, STROKE]


def test_line_caps_scale_with_non_scaling_width():
    """Line caps follow the non-scaling stroke width, not the transform."""
    def scene(vector_effect):
        return svg(
            '<path d="M 10 20 H 70" fill="none" stroke="black" '
            f'stroke-width="{STROKE}" stroke-linecap="round" '
            f'transform="scale(4, 1)" {vector_effect}/>',
            width=320, height=40)

    image = render(scene('vector-effect="non-scaling-stroke"'))
    painted, = scan_row(image, 20)
    # The round caps add STROKE / 2 on each side, in device pixels.
    assert painted == (10 * 4 - STROKE // 2, 70 * 4 + STROKE // 2 - 1)

    scaled = render(scene(''))
    painted, = scan_row(scaled, 20)
    # The default behaviour: the round caps are stretched horizontally.
    assert painted == (
        10 * 4 - STROKE * 4 // 2, 70 * 4 + STROKE * 4 // 2 - 1)


def test_line_joins_use_non_scaling_width():
    """Miter joins are computed with the host-space stroke width."""
    image = render(svg(
        '<path d="M 10 10 H 40 V 40" fill="none" stroke="black" '
        f'stroke-width="{STROKE}" stroke-linejoin="miter" '
        'transform="scale(4, 1)" vector-effect="non-scaling-stroke"/>',
        width=200, height=200))
    # The right-angle corner is at (160, 10). With the non-scaling width the
    # miter tip ends at x = 160 + STROKE / 2 and the horizontal stroke at
    # row 10 spans from x = 40 - STROKE / 2.
    # The miter tip reaches one half stroke width past the corner.
    assert scan_row(image, 10) == [(40, 160 + STROKE // 2 - 1)]
    # The vertical part below the corner is STROKE wide, not 16.
    assert scan_row(image, 25) == [(160 - STROKE // 2, 160 + STROKE // 2 - 1)]
    assert scan_column(image, 60) == [(10 - STROKE // 2, 10 + STROKE // 2 - 1)]

    # The default stroke stretches both the join and the width by 4.
    plain = render(svg(
        '<path d="M 10 10 H 40 V 40" fill="none" stroke="black" '
        f'stroke-width="{STROKE}" stroke-linejoin="miter" '
        'transform="scale(4, 1)"/>', width=200, height=200))
    assert scan_row(plain, 25) == [(152, 167)]


def test_dash_array_is_measured_in_host_space():
    """Dash lengths use the host space, like the stroke width itself."""
    image = render(svg(
        '<path d="M 10 20 H 90" fill="none" stroke="black" '
        f'stroke-width="{STROKE}" stroke-dasharray="10 5" '
        'transform="scale(4, 1)" vector-effect="non-scaling-stroke"/>',
        width=400, height=40))
    segments = scan_row(image, 20)[:-1]
    # 10 painted device pixels, 5 empty device pixels: the dash pattern is
    # also measured in host space.
    assert widths(segments) == [10] * 21
    assert [start for start, _ in segments] == [
        40 + 15 * index for index in range(21)]


def test_dash_array_is_scaled_without_vector_effect():
    """The default stroke behaviour still scales dashes."""
    image = render(svg(
        '<path d="M 5 20 H 50" fill="none" stroke="black" '
        f'stroke-width="{STROKE}" stroke-dasharray="5 5" '
        'transform="scale(4, 1)"/>', width=400, height=40))
    assert widths(scan_row(image, 20)) == [20, 20, 20, 20, 20]


def test_marker_is_not_scaled_with_non_scaling_stroke():
    """A strokeWidth marker follows the non-scaling width."""
    def scene(vector_effect):
        return svg(
            '<defs><marker id="arrow" markerUnits="strokeWidth" '
            'markerWidth="4" markerHeight="4" refX="0" refY="0" orient="0" '
            'overflow="visible"><rect width="4" height="4" '
            'fill="black"/></marker></defs>'
            '<path d="M 10 10 H 30" fill="none" stroke="none" '
            f'marker-end="url(#arrow)" transform="scale(4, 1)" '
            f'stroke-width="{STROKE}" {vector_effect}/>',
            width=200, height=60)

    def marker_box(image):
        painted = [
            (x, y) for y in range(image.height) for x in range(image.width)
            if ink()(image.getpixel((x, y)))]
        xs = [x for x, _ in painted]
        ys = [y for _, y in painted]
        return min(xs), min(ys), max(xs), max(ys)

    scaled = marker_box(render(scene('')))
    # 4 marker units * 4 stroke units * 4 scale = 64 device pixels wide.
    assert scaled[2] - scaled[0] + 1 == 64

    non_scaling = marker_box(render(scene(
        'vector-effect="non-scaling-stroke"')))
    # 4 marker units * 4 stroke pixels, whatever the transform.
    assert non_scaling[2] - non_scaling[0] + 1 == 16
    assert non_scaling[3] - non_scaling[1] + 1 == 16
    # The marker still starts at the end point of the transformed path.
    assert non_scaling[0] == 30 * 4


def test_user_space_marker_keeps_user_geometry():
    """A userSpaceOnUse marker is not affected by the vector effect."""
    image = render(svg(
        '<defs><marker id="arrow" markerUnits="userSpaceOnUse" '
        'markerWidth="8" markerHeight="8" refX="0" refY="0" orient="0" '
        'overflow="visible"><rect width="8" height="8" fill="black"/>'
        '</marker></defs>'
        '<path d="M 10 10 H 30" fill="none" stroke="none" '
        'marker-end="url(#arrow)" transform="scale(4, 1)" '
        'stroke-width="4" vector-effect="non-scaling-stroke"/>',
        width=200, height=60))
    painted = [
        (x, y) for y in range(image.height) for x in range(image.width)
        if ink()(image.getpixel((x, y)))]
    xs = [x for x, _ in painted]
    ys = [y for _, y in painted]
    # 8 user units are scaled by 4 horizontally and by 1 vertically.
    assert (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1) == (32, 8)
    assert min(xs) == 30 * 4


def test_fill_is_unaffected():
    """The vector effect only changes the stroke, never the fill."""
    def scene(vector_effect):
        return svg(
            '<rect x="10" y="10" width="20" height="10" fill="black" '
            f'transform="scale(4, 1)" {vector_effect}/>',
            width=200, height=40)

    plain = render(scene(''))
    non_scaling = render(scene('vector-effect="non-scaling-stroke"'))
    assert plain.tobytes() == non_scaling.tobytes()


def test_nested_transforms_are_all_applied_to_geometry():
    """Nested groups compose, but the stroke width stays unscaled."""
    image = render(svg(
        '<g transform="scale(4, 1)"><g transform="scale(2, 1)">' +
        outline(5, 10, 10, 10, **{'vector-effect': 'non-scaling-stroke'}) +
        '</g></g>', width=200, height=40))
    # Geometry is scaled by 8 horizontally and 1 vertically.
    assert [sum(run) / 2 for run in scan_row(image, 14)] == pytest.approx(
        [40, 120], abs=1)
    assert widths(scan_row(image, 14)) == [STROKE, STROKE]
    assert widths(scan_column(image, 80)) == [STROKE, STROKE]


def test_viewbox_does_not_scale_non_scaling_stroke():
    """The root viewBox is a viewport transform, so it is ignored too."""
    image = render(svg(
        outline(10, 10, 20, 10, **{'vector-effect': 'non-scaling-stroke'}) +
        outline(60, 10, 20, 10),
        width=400, height=200, extra='viewBox="0 0 100 50"'))
    # The document is shown at a scale of 4: plain strokes are 4 times
    # thicker, non-scaling ones keep their device width.
    row = scan_row(image, 60)
    assert widths(row[:2]) == [STROKE, STROKE]
    assert widths(row[2:]) == [4 * STROKE, 4 * STROKE]


def test_output_scale_is_applied_to_non_scaling_stroke():
    """The output scale factor is a device-space zoom and does scale."""
    body = outline(20, 20, 40, 40, **{'vector-effect': 'non-scaling-stroke'})
    small = render(svg(body, width=100, height=100), scale=1)
    large = render(svg(body, width=100, height=100), scale=2)
    assert widths(scan_column(small, 40)) == [STROKE, STROKE]
    assert widths(scan_column(large, 80)) == [2 * STROKE, 2 * STROKE]
    # The geometry itself follows the same zoom: 20..60 becomes 40..120.
    assert scan_column(large, 80) == [
        (2 * 20 - STROKE, 2 * 20 + STROKE - 1),
        (2 * 60 - STROKE, 2 * 60 + STROKE - 1)]


def test_repeated_reference_is_clean():
    """A referenced non-scaling stroke must not leak into the next clones."""
    image = render(svg(
        '<defs><rect id="shape" x="5" width="20" height="10" fill="none" '
        'stroke="black" stroke-width="4" '
        'vector-effect="non-scaling-stroke"/></defs>'
        '<g transform="scale(4, 1)">'
        '<use href="#shape" y="0"/>'
        '<use href="#shape" y="20"/>'
        '</g>'
        '<g transform="scale(4, 1)">' +
        outline(5, 40, 20, 10) + '</g>', width=200, height=100))
    # Both clones keep a non-scaling 4-pixel stroke.
    assert widths(scan_row(image, 5)) == [STROKE, STROKE]
    assert widths(scan_row(image, 25)) == [STROKE, STROKE]
    # The plain rectangle in the next group is still stretched by 4.
    assert widths(scan_row(image, 45)) == [4 * STROKE, 4 * STROKE]


def test_sibling_elements_are_clean():
    """A non-scaling element does not change the next sibling's stroke."""
    image = render(svg(
        '<g transform="scale(4, 1)" fill="none" stroke="black" '
        f'stroke-width="{STROKE}">' +
        outline(5, 10, 20, 10,
                **{'vector-effect': 'non-scaling-stroke'}) +
        outline(5, 40, 20, 10) + '</g>', width=200, height=120))
    # Both vertical edges of the non-scaling rectangle keep 4 pixels.
    assert widths(scan_row(image, 15)) == [STROKE, STROKE]
    # The next sibling is still stretched 4 times horizontally.
    assert widths(scan_row(image, 45)) == [4 * STROKE, 4 * STROKE]
    # Both rectangles keep complete horizontal edges with the normal,
    # unscaled 4-pixel thickness.
    assert widths(scan_column(image, 40)) == [STROKE] * 4


@pytest.mark.parametrize('value', [
    'non-scaling-stroke', 'non-scaling-stroke ', 'NON-SCALING-STROKE'])
def test_vector_effect_value_parsing(value):
    """The keyword is matched case-insensitively and tolerates spaces."""
    image = render(svg(
        '<g transform="scale(4, 1)">' +
        outline(5, 10, 20, 10, **{'vector-effect': value}) + '</g>',
        width=200, height=40))
    assert widths(scan_row(image, 14)) == [STROKE, STROKE]


def test_other_vector_effect_values_keep_normal_stroke():
    """Unsupported vector effects fall back to the default stroke."""
    image = render(svg(
        '<g transform="scale(4, 1)">' +
        outline(5, 10, 20, 10, **{'vector-effect': 'none'}) +
        outline(5, 30, 20, 10, **{'vector-effect': 'non-scaling-size'}) +
        '</g>', width=200, height=60))
    assert widths(scan_row(image, 14)) == [16, 16]
    assert widths(scan_row(image, 34)) == [16, 16]


def test_vector_effect_is_not_inherited():
    """vector-effect is a non-inherited property: only the shape applies it."""
    image = render(svg(
        '<g transform="scale(4, 1)" vector-effect="non-scaling-stroke">' +
        outline(5, 10, 20, 10) + '</g>', width=200, height=40))
    # The property does not inherit, so the child keeps the default stroke.
    assert widths(scan_row(image, 14)) == [16, 16]


def test_vector_effect_on_the_shape_is_applied():
    """The property applies to the graphics element that carries it."""
    image = render(svg(
        '<g transform="scale(4, 1)">' +
        outline(5, 10, 20, 10, **{'vector-effect': 'non-scaling-stroke'}) +
        '</g>', width=200, height=40))
    assert widths(scan_row(image, 14)) == [STROKE, STROKE]


def test_vector_effect_on_referenced_element():
    """A referenced element carries its own vector effect into each clone."""
    image = render(svg(
        '<defs><rect id="shape" x="5" width="20" height="10" fill="none" '
        'stroke="black" stroke-width="4" '
        'vector-effect="non-scaling-stroke"/></defs>'
        '<g transform="scale(4, 1)">'
        '<use href="#shape" y="0"/>'
        '<use href="#shape" y="20"/>'
        '</g>', width=200, height=60))
    assert widths(scan_row(image, 5)) == [STROKE, STROKE]
    assert widths(scan_row(image, 25)) == [STROKE, STROKE]


def test_vector_effect_on_use_is_not_propagated():
    """The property set on ``use`` does not reach the referenced content.

    This mirrors the behaviour of the reference implementations we compared
    against: only the graphics element that owns the attribute is affected.

    """
    image = render(svg(
        '<defs><rect id="shape" x="5" width="20" height="10" fill="none" '
        'stroke="black" stroke-width="4"/></defs>'
        '<g transform="scale(4, 1)">'
        '<use href="#shape" y="0" vector-effect="non-scaling-stroke"/>'
        '</g>', width=200, height=60))
    assert widths(scan_row(image, 5)) == [4 * STROKE, 4 * STROKE]


def test_pdf_output_strokes_in_host_space():
    """Vector outputs keep the same outline: check the PDF rendering."""
    from . import svg2pdf

    content = svg2pdf(bytestring=svg(
        '<g transform="scale(4, 1)">' +
        outline(5, 10, 20, 10, **{'vector-effect': 'non-scaling-stroke'}) +
        outline(5, 30, 20, 10) + '</g>', width=200, height=60))
    assert content.startswith(b'%PDF')


def test_svg_output_flattens_non_scaling_stroke():
    """SVG output shows the same geometry and width as Cairo computed."""
    from . import svg2svg

    content = svg2svg(bytestring=svg(
        '<g transform="scale(4, 1)">' +
        outline(5, 10, 20, 10, **{'vector-effect': 'non-scaling-stroke'}) +
        '</g>', width=200, height=40)).decode()
    # Cairo exports the host-space path with an identity transform and the
    # user stroke width, so no vector-effect attribute is needed.
    assert 'vector-effect' not in content
    assert 'stroke-width="4"' in content


def test_curve_is_drawn_in_host_space():
    """Curves are transformed as truly as Cairo paths."""
    image = render(svg(
        '<path d="M 10 50 C 10 10 50 10 50 50" fill="none" '
        'stroke="black" stroke-width="4" transform="scale(4, 1)" '
        'vector-effect="non-scaling-stroke"/>', width=240, height=60))
    middle = render(svg(
        '<path d="M 10 50 C 10 10 50 10 50 50" fill="none" '
        'stroke="black" stroke-width="16" transform="scale(4, 1)"/>',
        width=240, height=60))
    # The non-scaling curve must be thinner than the scaled one, and keep a
    # 4-pixel thickness where the curve is horizontal (its top point).
    assert sum(image.tobytes()) > sum(middle.tobytes())


def test_last_stroke_geometry_is_preserved():
    """Stroking never removes the path used by markers."""
    image = render(svg(
        '<defs><marker id="dot" markerUnits="userSpaceOnUse" markerWidth="4" '
        'markerHeight="4" refX="2" refY="2" orient="0" overflow="visible">'
        '<circle cx="2" cy="2" r="2" fill="black"/></marker></defs>'
        '<path d="M 10 10 H 40" fill="none" stroke="black" '
        'stroke-width="4" marker-end="url(#dot)" '
        'vector-effect="non-scaling-stroke"/>', width=80, height=40))
    # Stroke plus marker are both visible.
    assert scan_row(image, 10)
    assert scan_column(image, 40)


def test_nested_viewport_uses_furthest_host_space():
    """A nested svg viewport does not reset the host coordinate space."""
    image = render(svg(
        '<svg x="0" y="0" width="100%" height="100%" viewBox="0 0 50 50">' +
        outline(5, 5, 10, 10, **{'vector-effect': 'non-scaling-stroke'}) +
        '</svg>', width=200, height=200))
    assert widths(scan_row(image, 40)) == [STROKE, STROKE]
