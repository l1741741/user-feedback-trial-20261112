"""PNG pixel helpers shared by the rendering regression tests."""

import io

import cairocffi as cairo

from cairosvg import svg2png


def render(svg_text, width=200, height=200):
    """Render an SVG string to a dict of ``(x, y) -> (r, g, b, a)`` pixels."""
    if isinstance(svg_text, str):
        svg_text = svg_text.encode()
    png = svg2png(
        svg_text, output_width=width, output_height=height)
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png))
    width = surface.get_width()
    height = surface.get_height()
    pixels = {}
    buffer = surface.get_data()
    stride = surface.get_stride()
    # FORMAT_ARGB32 is stored as native-endian BGRA in memory.
    for y in range(height):
        row = buffer[y * stride:y * stride + width * 4]
        for x in range(width):
            b, g, r, a = row[4 * x:4 * x + 4]
            pixels[(x, y)] = (r, g, b, a)
    return pixels


def pixel(pixels, x, y):
    return pixels[(x, y)]


def max_channel_diff(pixels_a, pixels_b):
    """Largest per-channel difference between two equal-size pixel maps."""
    assert set(pixels_a) == set(pixels_b)
    return max(
        max(abs(channel_a - channel_b)
            for channel_a, channel_b in zip(pixels_a[key], pixels_b[key]))
        for key in pixels_a)


def assert_similar(pixels_a, pixels_b, tolerance=8):
    diff = max_channel_diff(pixels_a, pixels_b)
    assert diff <= tolerance, f'pixel mismatch: max channel diff = {diff}'


def count_opaque(pixels):
    return sum(1 for rgba in pixels.values() if rgba[3] > 10)


def alpha_at(pixels, x, y):
    return pixels[(x, y)][3]
