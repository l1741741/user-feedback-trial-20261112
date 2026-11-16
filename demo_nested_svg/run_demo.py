#!/usr/bin/env python3
"""Offline demo: render main.svg (which embeds two icons) and verify the
actual PNG pixels for color isolation, transparent areas and alignment.

The script only reads trusted local files next to this script and never
opens a network connection. Run it with the project virtualenv:

    python -I demo_nested_svg/run_demo.py
"""

import io
import os
import sys

from PIL import Image

from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface

HERE = os.path.dirname(os.path.abspath(__file__))


def trusted_local_fetcher(url, resource_type):
    """Fetcher restricted to file:// URLs inside this demo directory."""
    if not url.startswith('file://'):
        raise PermissionError(f'non-local URL refused: {url}')
    path = url[len('file://'):].split('#', 1)[0]
    root = os.path.realpath(HERE)
    if not os.path.realpath(path).startswith(root + os.sep):
        raise PermissionError(f'path outside demo directory refused: {url}')
    with open(path, 'rb') as file_object:
        return file_object.read()


def render_main():
    tree = Tree(
        url=os.path.join(HERE, 'main.svg'),
        url_fetcher=trusted_local_fetcher)
    output = io.BytesIO()
    PNGSurface(tree, output, 96).finish()
    png_bytes = output.getvalue()
    with open(os.path.join(HERE, 'main.png'), 'wb') as file_object:
        file_object.write(png_bytes)
    return Image.open(io.BytesIO(png_bytes)).convert('RGBA')


def close(pixel, rgba, tolerance=12):
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, rgba))


def main():
    image = render_main()
    assert image.size == (200, 160), image.size

    checks = []

    # 1. Each icon keeps its own gradient color (same id "paint").
    checks.append((
        'green icon keeps its own gradient',
        close(image.getpixel((55, 50)), (17, 177, 17))))
    checks.append((
        'orange icon keeps its own gradient',
        close(image.getpixel((145, 50)), (195, 110, 17))))

    # 2. The main document's same-id gradient stays blue.
    checks.append((
        'main rect uses the main blue gradient',
        close(image.getpixel((100, 122)), (8, 25, 178))))

    # 3. The green icon is clipped by its own circular clip path: the
    #    corner of its image region is transparent, not green.
    checks.append((
        'green icon clip leaves corners transparent',
        image.getpixel((24, 19)) == (0, 0, 0, 0)))
    # The orange icon uses a rounded-rect clip: corner transparent too.
    checks.append((
        'orange icon clip leaves corners transparent',
        image.getpixel((176, 19)) == (0, 0, 0, 0)))

    # 4. Alignment: icon content is centered inside the declared region,
    #    and the area between the two icons stays empty.
    checks.append((
        'gap between icons is transparent',
        image.getpixel((100, 50)) == (0, 0, 0, 0)))
    checks.append((
        'green icon center is opaque',
        image.getpixel((55, 50))[3] == 255))
    checks.append((
        'orange icon center is opaque',
        image.getpixel((145, 50))[3] == 255))

    # 5. Nothing leaks outside the document bounds.
    checks.append((
        'document corner is transparent',
        image.getpixel((2, 2)) == (0, 0, 0, 0)))

    width = max(len(name) for name, _ in checks)
    failures = 0
    for name, ok in checks:
        print(f'{"PASS" if ok else "FAIL"}  {name.ljust(width)}')
        failures += not ok
    print(f'\nWrote {os.path.join(HERE, "main.png")} ({image.size[0]}x'
          f'{image.size[1]})')
    return failures


if __name__ == '__main__':
    sys.exit(1 if main() else 0)
