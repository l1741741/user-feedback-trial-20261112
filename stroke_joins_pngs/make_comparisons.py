"""Build side-by-side "before / after" images from the sheet renders.

Usage::

    python stroke_joins_pngs/make_comparisons.py

Each output image stacks the original rendering above the fixed one and marks
which one is which, so the difference can be seen without opening both files.
"""

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
BEFORE = os.path.join(HERE, 'before')
AFTER = os.path.join(HERE, 'after')
OUTPUT = os.path.join(HERE, 'comparison')

BACKGROUND = (255, 255, 255)
LABEL_HEIGHT = 28
GAP = 8
BORDER = (200, 200, 200)


def label(text, width):
    band = Image.new('RGB', (width, LABEL_HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(band)
    draw.text((6, 8), text, fill=(0, 0, 0))
    return band


def stack(before, after, name):
    width = before.width + after.width + GAP
    height = max(before.height, after.height) + 2 * (LABEL_HEIGHT + GAP)
    image = Image.new('RGB', (width, height), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((6, 8), 'BEFORE (original code)', fill=(180, 0, 0))
    draw.text(
        (before.width + GAP + 6, 8), 'AFTER (fixed code)', fill=(0, 120, 0))
    image.paste(before, (0, LABEL_HEIGHT + GAP))
    image.paste(after, (before.width + GAP, LABEL_HEIGHT + GAP))
    draw.rectangle(
        (before.width + GAP // 2, 0, before.width + GAP // 2, height),
        fill=BORDER)
    image.save(os.path.join(OUTPUT, f'{name}.png'))


def failure_note(name, after):
    """Render a 'before' panel explaining that the old code crashed."""
    note = Image.new('RGB', (after.width, after.height), (255, 240, 240))
    draw = ImageDraw.Draw(note)
    lines = ['Rendering aborted before the fix.']
    failure_file = os.path.join(BEFORE, f'{name}-FAILED.txt')
    if os.path.exists(failure_file):
        with open(failure_file) as file:
            lines += file.read().strip().splitlines()
    for line_number, line in enumerate(lines):
        draw.text((10, 30 + 18 * line_number), line, fill=(160, 0, 0))
    return note


def main():
    os.makedirs(OUTPUT, exist_ok=True)
    names = sorted(
        file[:-4] for file in os.listdir(AFTER) if file.endswith('.png'))
    for name in names:
        after_path = os.path.join(AFTER, name + '.png')
        before_path = os.path.join(BEFORE, name + '.png')
        after = Image.open(after_path).convert('RGB')
        if os.path.exists(before_path):
            before = Image.open(before_path).convert('RGB')
        else:
            before = failure_note(name, after)
        stack(before, after, name)


if __name__ == '__main__':
    main()
