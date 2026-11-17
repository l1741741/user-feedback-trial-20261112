#!/usr/bin/env python3
"""Demo: export a normal and an inverted preview of the same SVG layout.

The layout (480x340) mixes vector shapes, a gradient, text, a
semi-transparent RGBA PNG, a palette PNG with a transparent index, an RGB
JPEG and a JPEG carrying EXIF orientation. It is exported four times:

    1_normal.png         both options off (original output)
    2_negate_colors.png  negate_colors=True  (vector colors complemented)
    3_invert_images.png  invert_images=True  (raster pixels complemented)
    4_both.png           both options combined

All inputs are generated locally into ``output/assets`` and the resulting
PNGs plus an ``index.html`` overview (inputs and results side by side) are
written into ``output/``. Run from the repository root:

    .venv/bin/python demo/demo_color_flags.py

Local image files are loaded through a scoped ``url_fetcher`` limited to
the assets directory: the demo does not need ``unsafe=True`` and the color
options never widen resource access.

"""

import io
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cairosvg.surface import PNGSurface  # noqa: E402

OUT = Path(__file__).resolve().parent / 'output'
ASSETS = OUT / 'assets'


def build_assets():
    """Generate the local raster material referenced by the layout."""
    ASSETS.mkdir(parents=True, exist_ok=True)

    # Semi-transparent RGBA PNG: opaque right half, alpha=128 left half,
    # fully transparent top-left corner.
    semi = Image.new('RGBA', (120, 90), (16, 90, 220, 255))
    for x in range(60):
        for y in range(90):
            semi.putpixel((x, y), (220, 40, 60, 128))
    for x in range(30):
        for y in range(30):
            semi.putpixel((x, y), (0, 0, 0, 0))
    semi.save(ASSETS / 'semi_transparent.png')

    # Palette PNG with a transparent index.
    palette = Image.new('P', (120, 90))
    palette.putpalette([30, 144, 255, 255, 140, 0] + [0, 0, 0] * 254)
    for x in range(120):
        for y in range(90):
            palette.putpixel((x, y), 0 if (x // 15 + y // 15) % 2 == 0 else 1)
    palette.info['transparency'] = bytes([255, 96])
    palette.save(ASSETS / 'palette_transparent.png')

    # Plain RGB JPEG "photo".
    photo = Image.new('RGB', (120, 90))
    for x in range(120):
        for y in range(90):
            photo.putpixel((x, y), (x * 2, 80 + y, 160))
    photo.save(ASSETS / 'photo_rgb.jpg', quality=92)

    # JPEG with EXIF orientation 6: stored 90x60, displayed rotated 60x90.
    rotated = Image.new('RGB', (90, 60))
    for x in range(90):
        for y in range(60):
            rotated.putpixel((x, y), (250, 200, 40) if x < 45
                             else (40, 60, 180))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise for display
    rotated.save(ASSETS / 'photo_exif.jpg', quality=92, exif=exif)


SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="480" height="340">
  <rect x="10" y="10" width="220" height="60" fill="#3c78b4"
        stroke="#112233" stroke-width="4"/>
  <defs>
    <linearGradient id="grad" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#ff7f00"/>
      <stop offset="1" stop-color="#007f7f"/>
    </linearGradient>
  </defs>
  <circle cx="340" cy="40" r="30" fill="url(#grad)"/>
  <text x="10" y="100" font-size="20" fill="#228b22">Vector text 矢量文字</text>
  <image href="semi_transparent.png" x="10" y="120" width="120" height="90"/>
  <image href="palette_transparent.png" x="150" y="120"
         width="120" height="90"/>
  <image href="photo_rgb.jpg" x="290" y="120" width="120" height="90"/>
  <image href="photo_exif.jpg" x="430" y="120" width="40" height="60"/>
  <image href="semi_transparent.png" x="10" y="230" width="180" height="100"
         preserveAspectRatio="none" opacity="0.9"/>
  <image href="palette_transparent.png" x="210" y="230" width="120" height="90"
         clip="rect(0px, 60px, 0px, 0px)"/>
</svg>"""


def scoped_fetcher(url, resource_type=None):
    """Allow reading files from the assets directory only."""
    path = Path(unquote(urlparse(url).path)).resolve()
    if path.is_file() and path.parent == ASSETS.resolve():
        return path.read_bytes()
    raise ValueError(f'resource not allowed: {url}')


def render(**kwargs):
    return PNGSurface.convert(
        bytestring=SVG.encode('utf8'), url=str(ASSETS / 'layout.svg'),
        url_fetcher=scoped_fetcher, **kwargs)


def pixel(png_bytes, xy):
    return Image.open(io.BytesIO(png_bytes)).getpixel(xy)


def main():
    build_assets()
    (ASSETS / 'layout.svg').write_text(SVG)

    variants = {
        '1_normal.png': {},
        '2_negate_colors.png': {'negate_colors': True},
        '3_invert_images.png': {'invert_images': True},
        '4_both.png': {'negate_colors': True, 'invert_images': True},
    }
    outputs = {name: render(**kwargs) for name, kwargs in variants.items()}
    for name, payload in outputs.items():
        (OUT / name).write_bytes(payload)

    checks = [
        ('vector rect complemented by negate_colors',
         pixel(outputs['2_negate_colors.png'], (30, 30))[:3] == (195, 135, 75)),
        ('vectors untouched by invert_images',
         pixel(outputs['3_invert_images.png'], (30, 30))[:3] == (60, 120, 180)),
        ('raster untouched by negate_colors',
         pixel(outputs['2_negate_colors.png'], (330, 150))[:3] ==
         pixel(outputs['1_normal.png'], (330, 150))[:3]),
        ('raster inverted by invert_images',
         pixel(outputs['3_invert_images.png'], (330, 150))[:3] ==
         tuple(255 - c for c in pixel(outputs['1_normal.png'], (330, 150))[:3])),
        ('transparent corner stays transparent (no black background)',
         all(pixel(png, (15, 125))[3] == 0 for png in outputs.values())),
        ('semi-transparent alpha preserved',
         abs(pixel(outputs['3_invert_images.png'], (40, 150))[3] - 128) <= 2),
        ('EXIF photo displayed rotated (taller than wide)',
         pixel(outputs['1_normal.png'], (445, 130))[:3] != (0, 0, 0)),
        ('repeat export is byte-identical (no double inversion)',
         render(invert_images=True) == outputs['3_invert_images.png']),
        ('plain export after inverted ones is unchanged',
         render() == outputs['1_normal.png']),
    ]

    rows = []
    ok = True
    for label, passed in checks:
        ok &= passed
        rows.append(f'  {"PASS" if passed else "FAIL"}  {label}')
    print('\n'.join(rows))

    images = ''.join(
        f'<figure><img src="{name}"><figcaption>{name}</figcaption></figure>'
        for name in outputs)
    inputs = ''.join(
        f'<figure><img src="assets/{name}"><figcaption>{name}</figcaption>'
        f'</figure>'
        for name in ('semi_transparent.png', 'palette_transparent.png',
                     'photo_rgb.jpg', 'photo_exif.jpg'))
    (OUT / 'index.html').write_text(f"""<!doctype html>
<meta charset="utf-8">
<title>CairoSVG negate_colors / invert_images demo</title>
<style>
  body {{ font-family: sans-serif; background: #888; }}
  figure {{ display: inline-block; margin: 8px; background: #fff;
           padding: 8px; }}
  img {{ image-rendering: pixelated; }}
  figcaption {{ font-size: 12px; }}
</style>
<h1>Inputs (local assets)</h1>
{inputs}
<h1>Outputs (this run)</h1>
{images}
<h2>Pixel checks</h2>
<pre>{chr(10).join(rows)}</pre>
""")

    print(f'\nAssets, PNG outputs and index.html written to {OUT}')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
