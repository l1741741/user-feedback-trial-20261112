"""Regression tests for the sizing, positioning and alignment of ``<image>``.

These tests render only local, self-generated raster assets (no network
access) and assert against geometry computed independently from the SVG 1.1
``preserveAspectRatio`` rules.

Covered requirements:

* meet / slice / none semantics in landscape, portrait and nested-transform
  containers;
* every legal alignment combination keeps the image inside its viewport;
* EXIF orientation is honored for PNG and JPEG alike and never mixed with the
  raster's real pixel size or the SVG-specified region;
* transparent pixels are alpha-composited instead of being filled black;
* repeated references to one image at different sizes are independent;
* explicit zero dimensions disable rendering, missing ones use intrinsic size;
* repeated conversions do not share mutable state.
"""

import io

import pytest
from PIL import Image, ImageDraw, ImageOps

from . import svg2png

RED = (255, 0, 0, 255)
GREEN = (0, 200, 0, 255)
BLUE = (0, 0, 255, 255)
YELLOW = (255, 200, 0, 255)
QUAD_COLORS = {'red': RED, 'green': GREEN, 'blue': BLUE, 'yellow': YELLOW}

# Source image: 40x30 with a 4px fully transparent border. Its opaque
# interior (32x22) is split into four 16x11 colored quadrants, and a white
# cross plus black dot marks the exact center.
SRC_W, SRC_H = 40, 30
BORDER = 4
# Points well inside each colored quadrant, clear of both the 4px
# transparent border and the white center cross / black center dot.
QUAD_SRC_POINT = {
    'red': (6, 6), 'green': (33, 6),
    'blue': (6, 23), 'yellow': (33, 23),
}

ALIGNS = [
    'xMinYMin', 'xMidYMin', 'xMaxYMin',
    'xMinYMid', 'xMidYMid', 'xMaxYMid',
    'xMinYMax', 'xMidYMax', 'xMaxYMax',
]


def make_marker_png(path):
    image = Image.new('RGBA', (SRC_W, SRC_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    x0 = y0 = BORDER
    x1, y1 = SRC_W - BORDER - 1, SRC_H - BORDER - 1
    quad_w, quad_h = (SRC_W - 2 * BORDER) // 2, (SRC_H - 2 * BORDER) // 2
    draw.rectangle((x0, y0, x0 + quad_w - 1, y0 + quad_h - 1), fill=RED)
    draw.rectangle((x0 + quad_w, y0, x1, y0 + quad_h - 1), fill=GREEN)
    draw.rectangle((x0, y0 + quad_h, x0 + quad_w - 1, y1), fill=BLUE)
    draw.rectangle((x0 + quad_w, y0 + quad_h, x1, y1), fill=YELLOW)
    cx, cy = SRC_W // 2 - 1, SRC_H // 2 - 1
    draw.line((cx - 5, cy, cx + 5, cy), fill=(255, 255, 255, 255))
    draw.line((cx, cy - 4, cx, cy + 4), fill=(255, 255, 255, 255))
    draw.rectangle((cx - 1, cy - 1, cx + 1, cy + 1), fill=(0, 0, 0, 255))
    image.save(path, 'PNG')
    return str(path)


def make_opaque_png(source, path):
    base = Image.new('RGBA', (SRC_W, SRC_H), (255, 0, 255, 255))
    base.alpha_composite(Image.open(source))
    base.save(path, 'PNG')


def make_exif(source, path, orientation, fmt):
    base = Image.open(source).convert('RGB')
    exif = base.getexif()
    exif[274] = orientation
    base.save(path, fmt, exif=exif)


@pytest.fixture(scope='module')
def marker(tmp_path_factory):
    path = tmp_path_factory.mktemp('assets') / 'markers.png'
    return make_marker_png(path)


@pytest.fixture(scope='module')
def opaque_marker(tmp_path_factory, marker):
    opaque = tmp_path_factory.mktemp('opaque') / 'opaque.png'
    make_opaque_png(marker, opaque)
    return str(opaque)


def render(body, width, height, flatten=True):
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">{body}</svg>')
    raw = Image.open(io.BytesIO(
        svg2png(bytestring=svg.encode(), unsafe=True))).convert('RGBA')
    if not flatten:
        return raw
    flat = Image.new('RGBA', raw.size, (255, 255, 255, 255))
    flat.alpha_composite(raw)
    return flat


def image_tag(href, x, y, w, h, par, extra=''):
    return (f'<image href="{href}" x="{x}" y="{y}" width="{w}" height="{h}" '
            f'preserveAspectRatio="{par}"{extra}/>')


def expected_layout(par, x, y, w, h):
    """Independent implementation of the preserveAspectRatio placement."""
    sx, sy = w / SRC_W, h / SRC_H
    align = par.split()[0]
    if align == 'none':
        return (sx, sy), x, y
    scale = min(sx, sy) if par.endswith('meet') else max(sx, sy)
    xpos, ypos = align[1:4].lower(), align[5:].lower()
    drawn_w, drawn_h = SRC_W * scale, SRC_H * scale
    tx = x + {'min': 0, 'mid': (w - drawn_w) / 2,
              'max': w - drawn_w}[xpos]
    ty = y + {'min': 0, 'mid': (h - drawn_h) / 2,
              'max': h - drawn_h}[ypos]
    return (scale, scale), tx, ty


def assert_color_close(actual, expected, tol=70):
    assert all(abs(a - b) <= tol
               for a, b in zip(actual[:3], expected[:3])), (actual, expected)


@pytest.mark.parametrize('align', ALIGNS)
@pytest.mark.parametrize('fit', ['meet', 'slice'])
def test_meet_slice_alignment_landscape(marker, align, fit):
    x, y, w, h = 20, 25, 80, 60
    par = f'{align} {fit}'
    image = render(image_tag(marker, x, y, w, h, par), 130, 110)
    (sx, sy), tx, ty = expected_layout(par, x, y, w, h)
    for name, (src_x, src_y) in QUAD_SRC_POINT.items():
        # Under slice the source quadrant may be cropped out of the region.
        if fit == 'slice':
            visible_x = x <= tx + src_x * sx < x + w
            visible_y = y <= ty + src_y * sy < y + h
            if not (visible_x and visible_y):
                continue
        px = round(tx + src_x * sx)
        py = round(ty + src_y * sy)
        assert_color_close(image.getpixel((px, py)), QUAD_COLORS[name])


@pytest.mark.parametrize('fit', ['meet', 'slice'])
def test_portrait_container(marker, fit):
    x, y, w, h = 40, 10, 30, 90
    par = f'xMidYMid {fit}'
    image = render(image_tag(marker, x, y, w, h, par), 110, 110)
    (sx, sy), tx, ty = expected_layout(par, x, y, w, h)
    for name, (src_x, src_y) in QUAD_SRC_POINT.items():
        if fit == 'slice' and not (
                x <= tx + src_x * sx < x + w and
                y <= ty + src_y * sy < y + h):
            continue
        assert_color_close(
            image.getpixel((round(tx + src_x * sx), round(ty + src_y * sy))),
            QUAD_COLORS[name])


def test_none_stretches_independently(marker):
    x, y, w, h = 20, 20, 80, 60
    image = render(image_tag(marker, x, y, w, h, 'none'), 130, 110)
    for name, (src_x, src_y) in QUAD_SRC_POINT.items():
        px = round(x + src_x * w / SRC_W)
        py = round(y + src_y * h / SRC_H)
        assert_color_close(image.getpixel((px, py)), QUAD_COLORS[name])


@pytest.mark.parametrize('par', [
    *(f'{a} meet' for a in ALIGNS),
    *(f'{a} slice' for a in ALIGNS), 'none'])
def test_never_paints_outside_viewport(opaque_marker, par):
    x, y, w, h, W, H = 30, 25, 70, 50, 160, 140
    image = render(image_tag(opaque_marker, x, y, w, h, par), W, H)
    for py in range(H):
        for px in range(W):
            if x <= px < x + w and y <= py < y + h:
                continue
            assert image.getpixel((px, py)) == (255, 255, 255, 255), (
                par, px, py, image.getpixel((px, py)))


def test_slice_crops_source_center(marker):
    # Wide 80x30 region for a 40x30 source: slice scale 2 -> drawn 80x60,
    # the vertical center crop is shown and the center marker stays centered.
    x, y = 20, 20
    image = render(image_tag(marker, x, y, 80, 30, 'xMidYMid slice'),
                   120, 70)
    assert image.getpixel((60, 35))[:3] == (0, 0, 0)
    # The region's top edge is an opaque source row, never the transparent
    # border composited as white.
    r, g, b, a = image.getpixel((60, 21))
    assert a == 255 and (r, g, b) != (255, 255, 255)


def test_meet_gutters_keep_backdrop(marker):
    image = render(
        f'<rect width="120" height="70" fill="white"/>'
        + image_tag(marker, 20, 20, 80, 30, 'xMidYMid meet'), 120, 70)
    assert image.getpixel((25, 35)) == (255, 255, 255, 255)
    assert image.getpixel((95, 35)) == (255, 255, 255, 255)


def test_transparent_border_alpha_composites(marker):
    image = render(
        '<rect width="60" height="50" fill="rgb(10,200,10)"/>'
        + image_tag(marker, 10, 10, 40, 30, 'xMidYMid meet'), 60, 50)
    # A source pixel inside the transparent border (device 11,11) must show
    # the green backdrop through real alpha, never an opaque black fill.
    assert image.getpixel((11, 11)) == (10, 200, 10, 255)
    raw = render(
        '<rect width="60" height="50" fill="rgb(10,200,10)"/>'
        + image_tag(marker, 10, 10, 40, 30, 'xMidYMid meet'), 60, 50,
        flatten=False)
    assert raw.getpixel((11, 11)) == (10, 200, 10, 255)
    # An opaque quadrant is still painted (red source point (8,7)).
    assert_color_close(image.getpixel((16, 16)), RED)


def test_nested_transformed_container(marker):
    body = (
        '<g transform="translate(80,10) rotate(90)">'
        + image_tag(marker, 0, 0, 90, 60, 'xMidYMid slice') + '</g>')
    image = render(body, 180, 160)
    # Rotated region device box: x in [20, 80], y in [10, 100].
    for py in range(160):
        for px in range(180):
            if 20 <= px <= 80 and 10 <= py <= 100:
                continue
            assert image.getpixel((px, py)) == (255, 255, 255, 255)


@pytest.mark.parametrize('orientation', range(1, 9))
@pytest.mark.parametrize('fmt,ext', [('PNG', 'png'), ('JPEG', 'jpg')])
def test_exif_orientation_matches_pillow(tmp_path, marker, orientation,
                                         fmt, ext):
    path = tmp_path / f'oriented.{ext}'
    make_exif(marker, path, orientation, fmt)
    reference = ImageOps.exif_transpose(Image.open(path)).convert('RGBA')
    rw, rh = reference.size
    image = render(
        image_tag(path, 0, 0, rw, rh, 'none'), rw, rh)
    worst = max(
        abs(a - b)
        for px in range(0, rw, 2) for py in range(0, rh, 2)
        for a, b in zip(image.getpixel((px, py))[:3],
                        reference.getpixel((px, py))[:3]))
    # JPEG is lossy; PNG must be pixel-identical after orientation.
    assert worst <= (45 if ext == 'jpg' else 2)


def test_repeated_references_are_independent(marker):
    body = (
        image_tag(marker, 5, 10, 40, 30, 'xMidYMid meet')
        + image_tag(marker, 60, 10, 80, 60, 'xMidYMid meet')
        + image_tag(marker, 155, 10, 20, 90, 'xMidYMid meet'))
    image = render(body, 220, 120)
    sx, sy = QUAD_SRC_POINT['red']
    # First image is 1:1.
    assert_color_close(image.getpixel((5 + sx, 10 + sy)), RED)
    # Second image scale 2.
    assert_color_close(image.getpixel((60 + 2 * sx, 10 + 2 * sy)), RED)
    # Third image: meet scale 0.5, drawn 20x15 vertically centered in 90.
    offset_y = (90 - 15) / 2
    assert_color_close(
        image.getpixel((round(155 + 0.5 * sx), round(10 + offset_y + 0.5 * sy))),
        RED)


def test_multiple_calls_have_no_shared_state(marker):
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="60" height="50">'
        f'<image href="{marker}" x="10" y="10" width="40" height="30"/></svg>')
    first = svg2png(bytestring=svg.encode(), unsafe=True)
    second = svg2png(bytestring=svg.encode(), unsafe=True)
    assert first == second
    # A different sizing on the same source must not be affected by the
    # previous conversion.
    other = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60">'
        f'<image href="{marker}" x="0" y="0" width="80" height="60" '
        f'preserveAspectRatio="none"/></svg>')
    rendered = render(image_tag(marker, 0, 0, 80, 60, 'none'), 80, 60)
    assert_color_close(rendered.getpixel((12, 12)), RED)
    assert svg2png(bytestring=other.encode(), unsafe=True)


@pytest.mark.parametrize('decl', [
    'width="0" height="30"', 'width="40" height="0"', 'width="0" height="0"'])
def test_explicit_zero_dimensions_render_nothing(marker, decl):
    image = render(
        '<rect width="60" height="60" fill="rgb(0,170,0)"/>'
        f'<image href="{marker}" x="5" y="5" {decl}/>', 60, 60)
    for px in range(0, 60, 7):
        for py in range(0, 60, 7):
            assert image.getpixel((px, py)) == (0, 170, 0, 255)


def test_missing_dimensions_use_intrinsic_size(marker):
    raw = render(
        f'<image href="{marker}" x="0" y="0"/>', SRC_W, SRC_H, flatten=False)
    reference = Image.open(marker).convert('RGBA')
    assert raw.getbbox() == reference.getbbox()
    assert raw.getpixel((10, 10))[:3] == RED[:3]


def test_height_only_keeps_intrinsic_width(marker):
    # height=60 with intrinsic 40x30: meet scale is 1, the 40x30 image is
    # vertically centered (offset 15) inside the 40x60 region at (5,5).
    image = render(
        '<rect width="100" height="80" fill="white"/>'
        f'<image href="{marker}" x="5" y="5" height="60"/>', 100, 80)
    # Red source point (6,6) maps to device (11,26).
    assert_color_close(image.getpixel((11, 26)), RED)


def test_opacity_keeps_alpha(marker):
    image = render(
        '<rect width="60" height="50" fill="white"/>'
        + image_tag(marker, 10, 10, 40, 30, 'xMidYMid meet',
                    ' opacity="0.5"'), 60, 50)
    # Red source point (6,6) -> device (16,16). Half-opacity red over white
    # blends to (255, ~127, ~127): the red channel stays saturated while the
    # two zero channels lift toward white, proving real alpha compositing.
    r, g, b, a = image.getpixel((16, 16))
    assert a == 255
    assert r >= 245
    assert 100 <= g <= 160 and 100 <= b <= 160
    assert r > g and r > b and abs(g - b) <= 3


def test_invert_map_image_applies_to_png(marker):
    # The PNG fast path used to bypass map_image; inversion must now reach
    # raster PNGs, turning red (255,0,0) into cyan (0,255,255).
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="60" height="50">'
        f'<image href="{marker}" x="10" y="10" width="40" height="30"/></svg>')
    image = Image.open(io.BytesIO(svg2png(
        bytestring=svg.encode(), unsafe=True,
        invert_images=True))).convert('RGBA')
    flat = Image.new('RGBA', image.size, (255, 255, 255, 255))
    flat.alpha_composite(image)
    # Red source point (6,6) maps to device (16,16).
    assert flat.getpixel((16, 16))[:3] == (0, 255, 255)
