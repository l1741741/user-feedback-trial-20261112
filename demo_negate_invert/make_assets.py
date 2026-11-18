"""Generate the local raster assets used by the colour-flags demo.

The assets are plain files written next to this script, so the demo and the
tests never touch the network and do not need binary fixtures in git.

Run with::

    python demo_negate_invert/make_assets.py

"""

from pathlib import Path

from PIL import Image

ASSETS = Path(__file__).parent / 'assets'

RED = (200, 30, 40, 255)
SEMI_GREEN = (10, 220, 50, 128)
TRANSPARENT_PURPLE = (90, 10, 160, 0)
YELLOW = (250, 240, 0, 255)


def build_rgba():
    """64×64 RGBA image with four quadrants of different alpha values."""
    image = Image.new('RGBA', (64, 64), TRANSPARENT_PURPLE)
    for y in range(64):
        for x in range(64):
            if y < 32:
                image.putpixel((x, y), RED if x < 32 else SEMI_GREEN)
            elif x >= 32:
                image.putpixel((x, y), YELLOW)
    image.save(ASSETS / 'rgba_quadrants.png')


def build_rgb():
    """64×64 opaque RGB image."""
    Image.new('RGB', (64, 64), (30, 120, 200)).save(ASSETS / 'rgb_blue.png')


def build_palette():
    """64×64 palette image whose second half is a transparent index."""
    image = Image.new('P', (64, 64), 0)
    image.putpalette([255, 0, 0, 0, 0, 255] + [0, 0, 0] * 254)
    image.paste(1, (32, 0, 64, 64))
    image.save(ASSETS / 'palette_transparent.png', transparency=1)


def build_rgb_with_trns():
    """RGB PNG using a tRNS colour key for transparency."""
    image = Image.new('RGB', (64, 64), (255, 0, 0))
    image.paste((0, 0, 0), (32, 0, 64, 64))
    image.save(ASSETS / 'rgb_colour_key.png', transparency=(0, 0, 0))


def build_oriented():
    """48×24 image with EXIF orientation 6 (rotate 90° clockwise).

    The left 24 columns are red, the right 24 columns are green.  Once the
    orientation is applied the red band is on top and the image is 24×48.
    """
    image = Image.new('RGB', (48, 24), (10, 200, 10))
    image.paste((220, 20, 20), (0, 0, 24, 24))
    exif = Image.Exif()
    exif[0x0112] = 6
    image.save(ASSETS / 'oriented.jpg', 'JPEG', quality=100, exif=exif)
    image.save(ASSETS / 'oriented.png', 'PNG', exif=exif)


def build_gray16():
    """64×64 16-bit grayscale PNG with a mid and a high sample."""
    from struct import pack
    from zlib import compress, crc32

    def chunk(chunk_type, data):
        content = chunk_type + data
        return (
            pack('>I', len(data)) + content + pack('>I', crc32(content)))

    rows = b''
    for _ in range(64):
        row = b'\x00'
        for x in range(64):
            row += pack('>H', 1000 if x < 32 else 65535)
        rows += row
    png = (
        b'\x89PNG\r\n\x1a\n' +
        chunk(b'IHDR', pack('>IIBBBBB', 64, 64, 16, 0, 0, 0, 0)) +
        chunk(b'IDAT', compress(rows)) +
        chunk(b'IEND', b''))
    (ASSETS / 'gray16.png').write_bytes(png)

def build_cmyk():
    """32×32 CMYK TIFF."""
    Image.new('CMYK', (32, 32), (0, 255, 255, 0)).save(ASSETS / 'cmyk.tif')


def build_cmyk_photo_like():
    """48×32 four-channel TIFF used in the demo layout."""
    image = Image.new('CMYK', (48, 32), (0, 200, 255, 0))
    for x in range(24, 48):
        for y in range(32):
            image.putpixel((x, y), (255, 0, 255, 0))
    image.save(ASSETS / 'cmyk_blocks.tif')


def build_demo_tiles():
    """Small 32×32 tile with a transparent border used for pattern/reuse."""
    tile = Image.new('RGBA', (32, 32), (0, 0, 0, 0))
    for y in range(4, 28):
        for x in range(4, 28):
            tile.putpixel((x, y), (255, 128, 0, 255))
    tile.save(ASSETS / 'tile_transparent.png')


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    build_rgba()
    build_rgb()
    build_palette()
    build_rgb_with_trns()
    build_oriented()
    build_gray16()
    build_cmyk()
    build_cmyk_photo_like()
    build_demo_tiles()
    print(f'Assets written to {ASSETS}')


if __name__ == '__main__':
    main()
