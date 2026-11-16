"""Guards for the regenerable regression images.

The PNGs in ``images/`` are build artifacts produced by
``generate_images.py``. These tests fail if a committed PNG no longer matches
what the current code renders from its companion SVG, which tells a developer
to regenerate the images rather than trust a stale picture.
"""

from pathlib import Path

from cairosvg import svg2png

from generate_images import FIXTURES, IMAGES


def test_every_fixture_has_svg_and_png():
    for name in FIXTURES:
        assert (IMAGES / f'{name}.svg').is_file()
        assert (IMAGES / f'{name}.png').is_file()


def test_committed_pngs_match_fresh_render():
    for name, svg in FIXTURES.items():
        committed = (IMAGES / f'{name}.png').read_bytes()
        # Re-render from the embedded SVG source rather than the committed
        # SVG file, so the test also catches a drifted generator.
        fresh_png = svg2png(svg.encode())
        assert committed == fresh_png, (
            f'{name}.png is stale; regenerate with '
            'python -I tests/object_bounds/generate_images.py')


def test_svg_sources_are_committed_verbatim():
    for name, svg in FIXTURES.items():
        assert (IMAGES / f'{name}.svg').read_text(encoding='utf-8') == svg
