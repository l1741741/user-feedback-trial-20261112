"""Render the ``paint-order`` demo and its zoomed-in areas.

Usage::

    python demo/render_demo.py

The input geometry is never modified: the same SVG is rendered once at
scale 2, and the PNG output is only cropped and scaled up to show the
overlapping areas.

"""

from pathlib import Path

from PIL import Image

import cairosvg

HERE = Path(__file__).resolve().parent
SOURCE = HERE / 'paint-order.svg'
OUTPUT = HERE / 'paint-order.png'
ZOOM = HERE / 'paint-order-zoom.png'

# The demo is rendered at scale 2; zoom boxes are given in SVG user units.
RENDER_SCALE = 2
ZOOM_REGIONS = (
    ('default', (10, 10, 110, 110)),
    ('stroke first', (150, 10, 250, 110)),
    ('markers first', (290, 10, 390, 110)),
    ('inherited / overridden', (430, 10, 550, 110)),
)
ZOOM_SCALE = 4


def main():
    """Render the demo SVG and write the PNG and zoom images."""
    png = cairosvg.svg2png(
        url=SOURCE, scale=RENDER_SCALE, background_color='white')
    OUTPUT.write_bytes(png)

    image = Image.open(OUTPUT).convert('RGB')
    crops = []
    for _, (left, top, right, bottom) in ZOOM_REGIONS:
        crop = image.crop(tuple(
            value * RENDER_SCALE for value in (left, top, right, bottom)))
        crops.append(crop.resize(
            (crop.width * ZOOM_SCALE, crop.height * ZOOM_SCALE),
            Image.NEAREST))

    # Put the four zooms side by side on a white background.
    margin = 16
    width = sum(crop.width for crop in crops) + margin * (len(crops) + 1)
    height = max(crop.height for crop in crops) + margin * 2
    zooms = Image.new('RGB', (width, height), 'white')
    x = margin
    for crop in crops:
        zooms.paste(crop, (x, margin))
        x += crop.width + margin
    zooms.save(ZOOM)

    print(f'Rendered {SOURCE.name} to {OUTPUT.name} '
          f'({image.width}x{image.height})')
    print(f'Wrote zoomed areas to {ZOOM.name} '
          f'({zooms.width}x{zooms.height})')
    for name, box in ZOOM_REGIONS:
        print(f'  zoom {name!r}: SVG region {box}')


if __name__ == '__main__':
    main()
