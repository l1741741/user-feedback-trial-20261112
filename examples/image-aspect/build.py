"""
Render the local examples for the ``image`` element geometry.

Run from the repository root:

    python examples/image-aspect/build.py

It writes, next to this script, the source thumbnail and one PNG per case.
No network resource is used: the raster input is generated here, and the
SVG document only refers to local files.
"""

import io
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

import cairosvg  # noqa: E402

BLUE = (0, 0, 255)
RED = (220, 20, 20)
GREEN = (20, 200, 20)
DARK_BLUE = (20, 20, 220)
YELLOW = (230, 220, 20)
MAGENTA = (230, 20, 230)


def make_thumbnail(path):
    """Write a 64x32 RGBA thumbnail with alpha, quadrants and a center mark.

    - a two-pixel fully transparent border, so the alpha compositing is
      visible against whatever is below the image;
    - four opaque colour quadrants, to read the alignment at a glance;
    - a magenta square in the middle, to read the scale and the crop.

    """
    image = Image.new('RGBA', (64, 32), (0, 0, 0, 0))
    for y in range(2, 30):
        for x in range(2, 62):
            if x < 32:
                colour = RED if y < 16 else DARK_BLUE
            else:
                colour = GREEN if y < 16 else YELLOW
            image.putpixel((x, y), (*colour, 255))
    for y in range(12, 20):
        for x in range(28, 36):
            image.putpixel((x, y), (*MAGENTA, 255))
    image.save(path)
    return image


CASES = {
    # Landscape viewport (200x140) and portrait viewport (100x200), each with
    # a ratio different from the 2:1 source, so that ``meet`` letterboxes,
    # ``slice`` crops and ``none`` stretches.
    'landscape-meet': ('x="30" y="30" width="220" height="140" '
                       'preserveAspectRatio="xMidYMid meet"'),
    'landscape-slice': ('x="30" y="30" width="220" height="140" '
                        'preserveAspectRatio="xMidYMid slice"'),
    'landscape-none': ('x="30" y="30" width="220" height="140" '
                       'preserveAspectRatio="none"'),
    'portrait-meet': ('x="80" y="30" width="100" height="200" '
                      'preserveAspectRatio="xMidYMid meet"'),
    'portrait-slice': ('x="80" y="30" width="100" height="200" '
                       'preserveAspectRatio="xMidYMid slice"'),
    'portrait-none': ('x="80" y="30" width="100" height="200" '
                      'preserveAspectRatio="none"'),
    # Alignment keywords on a square viewport, cropping the sides.
    'align-min': ('x="60" y="40" width="140" height="140" '
                  'preserveAspectRatio="xMinYMin slice"'),
    'align-max': ('x="60" y="40" width="140" height="140" '
                  'preserveAspectRatio="xMaxYMax slice"'),
    # A missing size is deduced from the image ratio.
    'auto-size': 'x="60" y="40" width="200"',
    # Rotation and nested transforms.
    'rotated': None,
    # A JPEG stored with an Exif orientation, drawn into a portrait viewport.
    'exif': None,
}

ROTATED = (
    '<g transform="translate(150,150) rotate(30) scale(1.4)">'
    '<image x="-100" y="-50" width="200" height="100" '
    'preserveAspectRatio="xMidYMid slice" href="thumbnail.png"/>'
    '</g>')

EXIF = (
    '<image x="80" y="30" width="100" height="200" '
    'preserveAspectRatio="xMidYMid meet" href="oriented.jpg"/>')

TEMPLATE = '''\
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">
  <rect width="100%" height="100%" fill="#f4f4f4"/>
  <rect width="100%" height="100%" fill="#0000ff" fill-opacity="0.06"/>
  <rect x="{box_x}" y="{box_y}" width="{box_width}" height="{box_height}"
        fill="none" stroke="#888888" stroke-width="1"
        stroke-dasharray="4 3"/>
  {content}
</svg>
'''


def make_oriented_jpeg(path):
    """Write a 64x32 JPEG whose Exif orientation rotates it 90° clockwise.

    The stored pixels are red on the left and blue on the right; once the
    orientation is applied, the image is displayed 32x64 with red on top.
    Ignoring the orientation would give the wrong size and the wrong crop.

    """
    image = Image.new('RGB', (64, 32), RED)
    for y in range(32):
        for x in range(32, 64):
            image.putpixel((x, y), DARK_BLUE)
    exif = Image.Exif()
    exif[0x0112] = 6
    image.save(path, exif=exif, quality=95)
    return image


def case_box(name, attributes):
    """Return the viewport ``(x, y, width, height)`` of a case."""
    if attributes is None:
        return (80, 30, 100, 200) if name == 'exif' else (50, 50, 200, 200)
    values = {}
    for part in attributes.split('" '):
        key, _, value = part.partition('="')
        values[key.strip()] = value.rstrip('"')
    x = float(values['x'])
    y = float(values['y'])
    width = float(values['width'])
    if 'height' in values:
        height = float(values['height'])
    else:
        # The example's source is 2:1.
        height = width / 2
    return x, y, width, height


def build():
    thumbnail = make_thumbnail(HERE / 'thumbnail.png')
    make_oriented_jpeg(HERE / 'oriented.jpg')
    for name, attributes in CASES.items():
        if attributes is None:
            if name == 'rotated':
                content = ROTATED
            else:
                content = EXIF
        else:
            content = f'<image {attributes} href="thumbnail.png"/>'
        box_x, box_y, box_width, box_height = case_box(name, attributes)
        document = TEMPLATE.format(
            width=300, height=300, box_x=box_x, box_y=box_y,
            box_width=box_width, box_height=box_height, content=content)
        source = HERE / f'{name}.svg'
        source.write_text(document)
        png = cairosvg.svg2png(
            url=source, unsafe=True, output_width=600, output_height=600)
        (HERE / f'{name}.png').write_bytes(png)
        print(f'wrote {name}.png from {name}.svg')

    # A contact sheet of the six main cases, for a single-file preview.
    columns, rows = 3, 2
    sheet = Image.new('RGBA', (columns * 300, rows * 300), (255, 255, 255))
    for index, name in enumerate((
            'landscape-meet', 'landscape-slice', 'landscape-none',
            'portrait-meet', 'portrait-slice', 'portrait-none')):
        picture = Image.open(HERE / f'{name}.png').convert('RGBA')
        picture.thumbnail((290, 290))
        x = (index % columns) * 300 + 5
        y = (index // columns) * 300 + 5
        sheet.alpha_composite(picture, (x, y))
    sheet.save(HERE / 'overview.png')
    print('wrote overview.png')
    return thumbnail


if __name__ == '__main__':
    build()
