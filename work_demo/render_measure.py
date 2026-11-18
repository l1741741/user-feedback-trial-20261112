"""Render an SVG to PNG and measure ink extents per horizontal band.

Usage: python render_measure.py <svg> <png> [band_y band_h ...]

For each band (y0, h) prints the left/right/top/bottom ink columns of dark
pixels, which lets tests check text chunk placement relative to guide lines
with an antialiasing tolerance.
"""
import io
import sys

sys.path.insert(0, '/Users/lichao/.local/share/user-feedback/graphics/20261117/rerun15-20261118-12585-text-chunk-anchor')

from PIL import Image

from cairosvg import svg2png


def ink_extents(img, y0, h, x0=0, w=None, alpha_min=160):
    """Extents of opaque black text ink.

    The PNG has a transparent background; grey/red guides are semi-transparent
    or colored, so we select opaque, neutral (black) pixels only.
    """
    if w is None:
        w = img.width
    px = img.load()
    minx, miny, maxx, maxy = img.width, h, -1, -1
    for y in range(max(0, y0), min(img.height, y0 + h)):
        for x in range(max(0, x0), min(img.width, x0 + w)):
            r, g, b, a = px[x, y]
            if (a >= alpha_min and max(r, g, b) < 100 and
                    max(r, g, b) - min(r, g, b) < 40):
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                rel_y = y - y0
                if rel_y < miny:
                    miny = rel_y
                if rel_y > maxy:
                    maxy = rel_y
    if maxx < 0:
        return None
    return minx, miny, maxx, maxy


def main():
    svg_path, png_path = sys.argv[1], sys.argv[2]
    with open(svg_path, 'rb') as fd:
        data = svg2png(bytestring=fd.read())
    with open(png_path, 'wb') as fd:
        fd.write(data)
    img = Image.open(io.BytesIO(data)).convert('RGBA')
    print(f'image {img.width}x{img.height}')
    args = [int(v) for v in sys.argv[3:]]
    for i in range(0, len(args), 4):
        x0, y0, w, h = args[i:i + 4]
        print(f'region x={x0} y={y0} w={w} h={h}: '
              f'{ink_extents(img, y0, h, x0, w)}')


if __name__ == '__main__':
    main()
