"""
Transform-origin / transform-box demo.

Renders a set of self-describing SVG scenes to PNG so the geometric results
can be eyeballed without opening a browser:

    .venv/bin/python demo_transform_origin.py            # writes demo_*.png
    .venv/bin/python demo_transform_origin.py --stdout   # prints the markup

Each scene is drawn from plain rectangles on purpose: the point of the demo is
to make centres, corners and layer order measurable with the naked eye, not to
showcase artwork.

The scenes are also asserted, to the pixel, by
``cairosvg/test_transform_origin.py``. Reference placements were cross-checked
against Chrome's ``getScreenCTM()``.
"""

import argparse
from pathlib import Path

from cairosvg import svg2png

OUTPUT_DIR = Path(__file__).parent / 'demo_pngs'

RED = '#d33'
BLUE = '#00b'
GREEN = '#2a8a3e'
INK = '#222'
MUTED = '#888'
PAPER = '#f5f5f5'


def scene_axes(width=420, height=260):
    """Draw the reference frame used by every other scene."""
    parts = [
        f'<rect width="{width}" height="{height}" fill="{PAPER}"/>',
    ]
    for x in range(0, width, 20):
        color = '#e2e2e2' if x % 100 else '#d0d0d0'
        parts.append(
            f'<line x1="{x}" y1="0" x2="{x}" y2="{height}" '
            f'stroke="{color}" stroke-width="1"/>')
    for y in range(0, height, 20):
        color = '#e2e2e2' if y % 100 else '#d0d0d0'
        parts.append(
            f'<line x1="0" y1="{y}" x2="{width}" y2="{y}" '
            f'stroke="{color}" stroke-width="1"/>')
    return ''.join(parts)


def label(x, y, text, size=11, color=INK, anchor='start'):
    return (
        f'<text x="{x}" y="{y}" font-family="sans-serif" font-size="{size}" '
        f'fill="{color}" text-anchor="{anchor}">{text}</text>')


def scene_centres():
    """Same rect and rotation, two different transform origins.

    Top: the initial ``0 0`` origin. Bottom: ``fill-box`` with ``50% 50%``.
    The dashed outline is the unrotated position; the blue dot marks the
    rotation origin.
    """
    width, height = 420, 260
    body = [
        f'<g>{scene_axes(width, height)}</g>',
        label(20, 20, 'default origin: 0 0 of the view box'),
        f'<rect x="120" y="30" width="100" height="24" fill="none" '
        f'stroke="{MUTED}" stroke-width="1" stroke-dasharray="4 3"/>',
        f'<rect x="120" y="30" width="100" height="24" fill="#d338" '
        f'stroke="{RED}" stroke-width="1" transform="rotate(20)"/>',
        f'<circle cx="0" cy="0" r="4" fill="{BLUE}"/>',

        label(20, 146, 'fill-box + transform-origin: 50% 50%'),
        f'<rect x="120" y="155" width="100" height="24" fill="none" '
        f'stroke="{MUTED}" stroke-width="1" stroke-dasharray="4 3"/>',
        f'<rect x="120" y="155" width="100" height="24" fill="#d338" '
        f'stroke="{RED}" stroke-width="1" transform="rotate(20)" '
        'style="transform-box:fill-box;transform-origin:50% 50%"/>',
        f'<circle cx="170" cy="167" r="4" fill="{BLUE}"/>',

        label(230, 20, 'swings away from its outline', size=10, color=MUTED),
        label(230, 146, 'rotates in place about its centre', size=10,
              color=MUTED),
        label(20, 246, 'dashed: unrotated outline; blue dot: rotation origin',
              color=MUTED),
    ]
    return wrap(body, width, height)


def scene_asymmetric():
    """One rectangle rotated by the same angle about four different origins.

    The blue dots are the rectangle's corners, so the rotation origin of each
    panel is visible: the shape swings around that corner. ``fill-box`` makes
    the percentages refer to the rectangle itself.
    """
    width, height = 440, 340
    body = [
        f'<g>{scene_axes(width, height)}</g>',
        label(20, 20, 'same 120x44 rect, rotate(35), four fill-box origins'),
    ]
    panels = (
        ('0 0', 40, 60), ('100% 0', 250, 60),
        ('0 100%', 40, 200), ('100% 100%', 250, 200))
    for text, x, y in panels:
        body.append(f'<g transform="translate({x},{y})">')
        body.append(
            f'<rect width="120" height="44" fill="none" stroke="{MUTED}" '
            'stroke-width="1" stroke-dasharray="4 3"/>')
        body.append(
            f'<rect width="120" height="44" fill="#d338" stroke="{RED}" '
            'stroke-width="1" transform="rotate(35)" '
            f'style="transform-box:fill-box;transform-origin:{text}"/>')
        for cx, cy in ((0, 0), (120, 0), (120, 44), (0, 44)):
            body.append(
                f'<circle cx="{cx}" cy="{cy}" r="3" fill="{BLUE}"/>')
        body.append(label(0, -8, f'origin: {text}', size=10, color=MUTED))
        body.append('</g>')
    body.append(label(
        20, 326, 'dashed: unrotated outline; blue dots: box corners',
        color=MUTED))
    return wrap(body, width, height)


def scene_layers():
    """Nested groups with their own origins; each layer is translucent.

    Outer group: rotate(15) about its own fill-box centre.
    Inner group: rotate(-30) about its own fill-box centre, then translated.
    The order of the fills shows which transform is the outer one.
    """
    width, height = 420, 260
    body = [
        f'<g>{scene_axes(width, height)}</g>',
        label(20, 24, 'nested layers: outer 15deg, inner -30deg, own centres'),
    ]
    body.append(
        '<g transform="rotate(15)" '
        'style="transform-box:fill-box;transform-origin:50% 50%">')
    body.append(
        f'<rect x="90" y="70" width="240" height="140" fill="#2a8a3e22" '
        f'stroke="{GREEN}" stroke-width="2"/>')
    body.append(
        '<g transform="rotate(-30) translate(40,0)" '
        'style="transform-box:fill-box;transform-origin:50% 50%">')
    body.append(
        f'<rect x="150" y="110" width="120" height="60" fill="#d338" '
        f'stroke="{RED}" stroke-width="2"/>')
    body.append(
        f'<circle cx="210" cy="140" r="3" fill="{BLUE}"/>')
    body.append('</g>')
    body.append('</g>')
    body.append(label(20, 250, 'green: outer group box; red: inner group',
                      color=MUTED))
    return wrap(body, width, height)


def scene_ordering():
    """The same two operations in one list, written in both orders.

    ``translate(180,40) scale(1.4)`` scales the rect first and then moves it,
    so its top-left corner lands exactly on the marker at (180, 40).
    ``scale(1.4) translate(180,40)`` moves first and then scales, so both the
    offset and the corner are multiplied by 1.4.
    """
    width, height = 470, 240
    body = [
        f'<g>{scene_axes(width, height)}</g>',
        label(20, 20, 'same rect at (0,0) and same numbers, list order '
                      'reversed'),
        # Red: scale first, then translate: corner (0,0) -> (180,40).
        f'<rect width="60" height="30" fill="#d338" stroke="{RED}" '
        'transform="translate(180,40) scale(1.4)"/>',
        f'<circle cx="180" cy="40" r="4" fill="{BLUE}"/>',
        label(20, 70, 'translate(180,40) scale(1.4)', size=10, color=MUTED),
        label(20, 86, 'corner lands on the blue dot', size=10, color=MUTED),

        # Green: translate first, then scale: (180,40) -> (252, 56).
        f'<rect width="60" height="30" fill="#2a8a3e88" stroke="{GREEN}" '
        'transform="scale(1.4) translate(180,40)"/>',
        f'<circle cx="252" cy="56" r="4" fill="{BLUE}"/>',
        label(272, 58, 'both coordinates times 1.4', size=10, color=MUTED),
        label(20, 176, 'scale(1.4) translate(180,40)', size=10, color=MUTED),
        label(20, 192, 'the offset and the corner are both scaled',
              size=10, color=MUTED),
        label(20, 222, 'dashed grid: 20px; blue dots: the rect corner',
              color=MUTED),
    ]
    return wrap(body, width, height)


def scene_use_reuse():
    """The same referenced node used twice, with different origins."""
    width, height = 420, 260
    body = [f'<g>{scene_axes(width, height)}</g>',
            label(20, 24, 'one referenced rect, two placements, no shared state')]
    body.append(
        '<defs><rect id="unit" x="0" y="0" width="80" height="30" '
        f'fill="#d338" stroke="{RED}" stroke-width="1"/></defs>')
    body.append(
        '<use href="#unit" x="60" y="80" transform="rotate(25)" '
        'style="transform-box:fill-box;transform-origin:50% 50%"/>')
    # 80x30 at (60,80): centre (100, 95), top-left (60, 80).
    body.append(
        f'<circle cx="100" cy="95" r="3" fill="{BLUE}"/>')
    body.append(
        '<use href="#unit" x="240" y="160" transform="rotate(-25)" '
        'style="transform-box:fill-box;transform-origin:0 0"/>')
    body.append(
        f'<circle cx="240" cy="160" r="3" fill="{BLUE}"/>')
    body.append(label(20, 240, 'left: origin at the box centre; '
                              'right: origin at the box corner',
                      color=MUTED))
    return wrap(body, width, height)


def wrap(body, width, height):
    inner = ''.join(body)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}">'
        f'{inner}</svg>')


SCENES = {
    'centres': scene_centres,
    'asymmetric': scene_asymmetric,
    'layers': scene_layers,
    'ordering': scene_ordering,
    'use_reuse': scene_use_reuse,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--stdout', action='store_true',
        help='print the generated SVG instead of writing PNG files')
    parser.add_argument(
        '--output', type=Path, default=OUTPUT_DIR,
        help='directory receiving the PNG files (default: demo_pngs)')
    args = parser.parse_args()

    if args.stdout:
        for name, scene in SCENES.items():
            print(f'<!-- {name} -->')
            print(scene())
        return

    args.output.mkdir(parents=True, exist_ok=True)
    for name, scene in SCENES.items():
        source = scene().encode()
        target = args.output / f'transform_origin_{name}.png'
        with target.open('wb') as output:
            svg2png(bytestring=source, write_to=output)
        print(f'{target} ({len(source)} bytes of SVG)')


if __name__ == '__main__':
    main()
