"""
Tests for the ``vector-effect: non-scaling-stroke`` presentation attribute.

The reference behavior, checked against actual rendered pixels, is that a
non-scaling stroke keeps a constant screen thickness for every transform
(translation, rotation, uniform and non-uniform scaling) while the path
geometry keeps following the object, group and viewport transforms. Plain
strokes keep their historical, transform-scaled behavior.

"""

import io
import shutil
import subprocess

import pytest

from . import svg2pdf, svg2png, svg2ps, svg2svg

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

requires_pil = pytest.mark.skipif(Image is None, reason='Pillow is required')

# Same 100x100 square outline, only the transform changes.
SHAPE = (
    '<rect x="-25" y="-25" width="50" height="50" fill="none" '
    'stroke="black" stroke-width="{width}"{nss}/>')

TRANSFORMS = {
    'translate': 'translate(110, 100)',
    'rotate': 'translate(110, 100) rotate(30)',
    'uniform': 'translate(110, 100) scale(2, 2)',
    'anisotropic': 'translate(110, 100) scale(2, 3)',
}


def _svg(transform, nss, width=4):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="220" height="200"
 viewBox="0 0 220 200">
  <rect width="220" height="200" fill="white"/>
  <g transform="{transform}">{SHAPE.format(
        width=width,
        nss=' vector-effect="non-scaling-stroke"' if nss else '')}</g>
</svg>'''.encode()


def _render(svg):
    return Image.open(io.BytesIO(svg2png(svg))).convert('RGB')


def _max_run(values):
    best = current = 0
    for value in values:
        current = current + 1 if value else 0
        best = max(best, current)
    return best


def _thickness_along_normal(image, point, direction, reach=10):
    """Measure ink thickness scanning along ``direction`` through ``point``.

    The point is expected to sit on a stroked edge and the direction to be a
    unit vector normal to that edge, so the ink run length is the local stroke
    thickness. Works for both axis-aligned and rotated edges.

    """
    pixels = image.load()
    dx, dy = direction
    px, py = point
    # Near-black threshold: count only the stroke core so anti-aliasing does
    # not inflate the measured geometric thickness on diagonal edges.
    values = [
        sum(pixels[round(px + dx * step), round(py + dy * step)]) < 250
        for step in range(-reach, reach + 1)]
    return _max_run(values)


def _thickness_on_edge(image, midpoint, tangent, normal, length=24):
    """Median normal-direction thickness sampled along a straight edge.

    Sampling several parallel scans away from the joins avoids corner artifacts
    on rotated edges and gives a robust thickness for a diagonal stroke.

    """
    tx, ty = tangent
    readings = []
    for offset in range(-length // 2, length // 2 + 1, 2):
        point = (midpoint[0] + tx * offset, midpoint[1] + ty * offset)
        readings.append(_thickness_along_normal(image, point, normal))
    return sorted(readings)[len(readings) // 2]


def _edge_thickness(image, vertical_edge_x, horizontal_edge_y):
    """Return (vertical edge, horizontal edge) thickness in pixels.

    Vertical edges are sampled by scanning columns through the square center,
    horizontal edges by scanning rows through the square center. Anti-aliased
    pixels are counted as ink.

    """
    vertical = _thickness_along_normal(
        image, (vertical_edge_x, 100), (1, 0))
    horizontal = _thickness_along_normal(
        image, (110, horizontal_edge_y), (0, 1))
    return vertical, horizontal


# Expected device-space location of the left and top edges of the square.
EDGE_POSITIONS = {
    'translate': (85, 75),
    'uniform': (60, 50),
    'anisotropic': (60, 25),
}

# For the rotated square, edges are diagonal: scan along their normals at the
# midpoint of two perpendicular edges (square centered on (110, 100), rotated
# by 30 degrees, half extent 25). Each entry gives the edge midpoint, the edge
# tangent and the outward normal, all unit vectors where applicable.
ROTATE_SCANS = (
    # user-space top edge: midpoint (0, -25), tangent along +x
    ((122.5, 78.35), (0.8660254, 0.5), (-0.5, 0.8660254)),
    # user-space left edge: midpoint (-25, 0), tangent along -x
    ((88.35, 87.5), (-0.8660254, 0.5), (0.5, 0.8660254)),
)


@requires_pil
@pytest.mark.parametrize('transform', TRANSFORMS)
def test_non_scaling_stroke_is_uniform(transform):
    image = _render(_svg(TRANSFORMS[transform], nss=True))
    if transform == 'rotate':
        thicknesses = [
            _thickness_on_edge(image, midpoint, tangent, normal)
            for midpoint, tangent, normal in ROTATE_SCANS]
    else:
        edge_x, edge_y = EDGE_POSITIONS[transform]
        thicknesses = list(_edge_thickness(image, edge_x, edge_y))
    # A 4 user-unit stroke is 4 pixels thick on PNG, in every direction.
    assert thicknesses
    assert all(abs(thickness - 4) <= 1 for thickness in thicknesses)


@requires_pil
def test_plain_stroke_keeps_scaling():
    # Non-uniform scale (2, 3): the plain stroke is 8 px wide on vertical
    # edges and 12 px tall on horizontal edges.
    image = _render(_svg(TRANSFORMS['anisotropic'], nss=False))
    vertical, horizontal = _edge_thickness(image, 60, 25)
    assert abs(vertical - 8) <= 1
    assert abs(horizontal - 12) <= 1


@requires_pil
def test_non_scaling_inherited_from_group():
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="220" height="200">
      <rect width="220" height="200" fill="white"/>
      <g transform="translate(110, 100) scale(2, 3)"
         vector-effect="non-scaling-stroke">
        <rect x="-25" y="-25" width="50" height="50" fill="none"
              stroke="black" stroke-width="4"/>
      </g></svg>'''
    vertical, horizontal = _edge_thickness(_render(svg), 60, 25)
    assert abs(vertical - 4) <= 1
    assert abs(horizontal - 4) <= 1


@requires_pil
def test_state_does_not_leak_between_objects():
    # An NSS object followed by a plain object: the second one must use the
    # normal, transform-scaled thickness.
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200">
      <rect width="320" height="200" fill="white"/>
      <g transform="translate(80,100) scale(2,3)">
        <rect x="-25" y="-25" width="50" height="50" fill="none"
              stroke="black" stroke-width="4"
              vector-effect="non-scaling-stroke"/>
      </g>
      <g transform="translate(240,100) scale(2,3)">
        <rect x="-25" y="-25" width="50" height="50" fill="none"
              stroke="black" stroke-width="4"/>
      </g></svg>'''
    image = _render(svg)
    nss = _edge_thickness(image.crop((0, 0, 160, 200)), 30, 25)
    plain = _edge_thickness(image.crop((160, 0, 320, 200)), 30, 25)
    assert abs(nss[0] - 4) <= 1 and abs(nss[1] - 4) <= 1
    assert abs(plain[0] - 8) <= 1 and abs(plain[1] - 12) <= 1


@requires_pil
def test_nested_transforms_and_repeated_use():
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="320" height="200"
 xmlns:xlink="http://www.w3.org/1999/xlink">
      <defs>
        <rect id="r" x="-25" y="-25" width="50" height="50" fill="none"
              stroke="black" stroke-width="4"
              vector-effect="non-scaling-stroke"/>
      </defs>
      <rect width="320" height="200" fill="white"/>
      <g transform="translate(80,100) scale(2,1)">
        <g transform="scale(1,3)"><use href="#r"/></g>
      </g>
      <g transform="translate(240,100) scale(2,3)"><use href="#r"/></g>
    </svg>'''
    image = _render(svg)
    first = _edge_thickness(image.crop((0, 0, 160, 200)), 30, 25)
    second = _edge_thickness(image.crop((160, 0, 320, 200)), 30, 25)
    for vertical, horizontal in (first, second):
        assert abs(vertical - 4) <= 1
        assert abs(horizontal - 4) <= 1


@requires_pil
def test_geometry_is_not_moved():
    # The centerline must stay identical with and without the effect; only
    # the stroke thickness changes, so the ink bounding boxes differ by at
    # most the thickness difference.
    path = 'M20,40 L60,40 L60,90 L110,90'
    template = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="180" height="150">'
        '<rect width="180" height="150" fill="white"/>'
        '<g transform="translate(10,10) scale(1.5,1.2)">'
        '<path d="{path}" fill="none" stroke="black" stroke-width="6"{nss}/>'
        '</g></svg>')

    def bbox(svg):
        image = _render(svg.encode())
        pixels = image.load()
        xs, ys = [], []
        for x in range(image.width):
            for y in range(image.height):
                if sum(pixels[x, y]) < 700:
                    xs.append(x)
                    ys.append(y)
        return min(xs), max(xs), min(ys), max(ys)

    plain = bbox(template.format(path=path, nss=''))
    nss = bbox(template.format(
        path=path, nss=' vector-effect="non-scaling-stroke"'))
    assert all(abs(a - b) <= 6 for a, b in zip(plain, nss))


@requires_pil
def test_marker_size_is_unchanged():
    # Markers follow their own markerUnits sizing and must not be resized by
    # the non-scaling stroke handling.
    template = (
        b'''<svg xmlns="http://www.w3.org/2000/svg" width="200" '''
        b'''height="120"
 xmlns:xlink="http://www.w3.org/1999/xlink">
      <defs><marker id="m" markerWidth="8" markerHeight="8" refX="4" refY="4">
        <path d="M0,0 L8,4 L0,8 z" fill="black"/>
      </marker></defs>
      <rect width="200" height="120" fill="white"/>
      <g transform="translate(30,60) scale(2,1)">
        <path d="M0,0 L60,0" stroke="black" stroke-width="4" fill="none"
              marker-end="url(#m)"{nss}/>
      </g></svg>''')

    def dark_count(svg):
        image = _render(svg)
        pixels = image.load()
        return sum(
            1 for x in range(140, 175) for y in range(40, 80)
            if sum(pixels[x, y]) < 700)

    nss = template.replace(b'{nss}', b' vector-effect="non-scaling-stroke"')
    plain = template.replace(b'{nss}', b'')
    assert dark_count(nss) == dark_count(plain)


@pytest.mark.parametrize('converter', (svg2pdf, svg2ps, svg2svg))
def test_vector_outputs_are_produced(converter):
    svg = _svg(TRANSFORMS['anisotropic'], nss=True)
    data = converter(svg)
    assert data[:5] in (b'%PDF-', b'%!PS-', b'<?xml')


def _rasterize_pdf(pdf_bytes, tmp_path):
    """Rasterize PDF bytes to a PIL image with pdftoppm, or return None."""
    pdftoppm = shutil.which('pdftoppm')
    if pdftoppm is None:
        return None
    pdf_path = tmp_path / 'drawing.pdf'
    pdf_path.write_bytes(pdf_bytes)
    subprocess.run(
        [pdftoppm, '-png', '-r', '96', str(pdf_path),
         str(tmp_path / 'page')],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pages = sorted(tmp_path.glob('page*.png'))
    if not pages:  # pragma: no cover - depends on poppler
        return None
    return Image.open(pages[0]).convert('RGB')


@requires_pil
def test_pdf_output_keeps_constant_thickness(tmp_path):
    # The same anisotropic scene as the PNG tests, exported to PDF (a vector
    # surface whose user unit is a point, not a pixel).
    svg = _svg(TRANSFORMS['anisotropic'], nss=True)
    image = _rasterize_pdf(svg2pdf(svg), tmp_path)
    if image is None:  # pragma: no cover - poppler unavailable
        pytest.skip('pdftoppm (poppler) is not installed')
    vertical, horizontal = _edge_thickness(image, 60, 25)
    # At 96 DPI a 4 user-unit stroke is 4 pixels, constant in both directions.
    assert abs(vertical - 4) <= 1
    assert abs(horizontal - 4) <= 1


@requires_pil
@pytest.mark.parametrize('gradient_units', ('objectBoundingBox',
                                            'userSpaceOnUse'))
def test_gradient_paint_still_follows_geometry(gradient_units):
    # The gradient must span the shape exactly as it does for a plain stroke:
    # red at the top edge, blue at the bottom edge. This guards the pattern
    # matrix handling when the stroke is painted under an identity CTM.
    if gradient_units == 'userSpaceOnUse':
        gradient = (
            '<linearGradient id="g" gradientUnits="userSpaceOnUse" '
            'x1="0" y1="20" x2="0" y2="200">')
    else:
        gradient = (
            '<linearGradient id="g" x1="0" y1="0" x2="0" y2="1">')
    template = (
        f'''<svg xmlns="http://www.w3.org/2000/svg" '''
        f'''width="220" height="220">
      <defs>{gradient}
        <stop offset="0" stop-color="red"/>
        <stop offset="1" stop-color="blue"/>
      </linearGradient></defs>
      <rect width="220" height="220" fill="white"/>
      <g transform="translate(110,110) scale(2,3)">
        <rect x="-30" y="-30" width="60" height="60" fill="none"
              stroke="url(#g)" stroke-width="8"{{nss}}/>
      </g></svg>''')

    def colors(svg):
        pixels = _render(svg).load()
        # The gradient runs vertically, so sample the horizontal top and bottom
        # edges (device y ~20 and ~200), well inside their stroke bands.
        return pixels[110, 22], pixels[110, 198]

    plain = template.format(nss='').encode()
    nss = template.format(
        nss=' vector-effect="non-scaling-stroke"').encode()
    # The non-scaling stroke must reproduce the plain stroke's paint exactly.
    assert colors(nss) == colors(plain)
    top, bottom = colors(nss)
    assert top[0] > top[2]                       # red dominant at the top
    assert bottom[2] > top[2]                    # blue grows toward the bottom
    if gradient_units == 'objectBoundingBox':
        assert bottom[2] > bottom[0]             # blue dominant at the bottom


def test_unknown_keyword_is_ignored():
    # Only non-scaling-stroke is recognized; other vector-effect values keep
    # the plain scaling behavior.
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="220" height="200">
      <rect width="220" height="200" fill="white"/>
      <g transform="translate(110,100) scale(2,3)">
        <rect x="-25" y="-25" width="50" height="50" fill="none"
              stroke="black" stroke-width="4"
              vector-effect="non-scaling-size other"/>
      </g></svg>'''
    vertical, horizontal = _edge_thickness(_render(svg), 60, 25)
    assert abs(vertical - 8) <= 1
    assert abs(horizontal - 12) <= 1
