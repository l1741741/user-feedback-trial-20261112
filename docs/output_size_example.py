"""
Runnable example: how output sizes map to the rendered PNG.

Run it from the repository root with::

    python docs/output_size_example.py

It prints the actual PNG size and the alpha value of the four border pixels
for a couple of representative requests, and shows how to ask for an output
size and check its result from your own code.

"""

from io import BytesIO

from PIL import Image

from cairosvg import svg2png

# A font-free, full-bleed document: its content covers the whole viewport, so
# any transparent border pixel means the viewport does not match the surface.
SQUARE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="1cm" height="1cm" '
    'viewBox="0 0 24 24">'
    '<rect width="24" height="24" fill="#ff0000"/>'
    '</svg>')

REQUESTS = (
    {},                          # 1cm at 96 dpi → 37.795… px → 38×38
    {'dpi': 72},                 # 1cm at 72 dpi → 28.346… px → 28×28
    {'output_width': 37.5},      # fractional request → rounded to 38×38
    {'scale': 1.37},             # fractional scale → rounded once
    {'output_width': 48, 'output_height': 24},  # both sizes are honoured
)

REQUEST_KEEPS_RATIO = (
    {}, {'dpi': 72}, {'output_width': 37.5}, {'scale': 1.37})


def border_alphas(png):
    """Return ``(width, height, (top, right, bottom, left))`` alphas."""
    image = Image.open(BytesIO(png)).convert('RGBA')
    width, height = image.size
    pixels = image.load()

    def alpha(x, y):
        """Return the alpha channel of the pixel at ``x``, ``y``."""
        return pixels[x, y][3]

    return (
        width, height,
        (alpha(width // 2, 0), alpha(width - 1, height // 2),
         alpha(width // 2, height - 1), alpha(0, height // 2)))


def main():
    """Render a few sizes and check the border pixels of each result."""
    for request in REQUESTS:
        png = svg2png(SQUARE, **request)
        width, height, (top, right, bottom, left) = border_alphas(png)
        print(
            f'{request!s:52} -> {width}x{height}, '
            f'border alpha (t/r/b/l) = {top}/{right}/{bottom}/{left}')
        if request in REQUEST_KEEPS_RATIO:
            # The document's own aspect ratio is kept, so the content must
            # reach all four edges of the rounded surface.
            assert (top, right, bottom, left) == (255, 255, 255, 255)
        else:
            # Both sizes were requested with different ratios: the default
            # preserveAspectRatio letterboxes the document, so the left and
            # right columns are transparent by design while the top and
            # bottom edges are still painted.
            assert (top, bottom) == (255, 255)
            assert (left, right) == (0, 0)

    # Checking the actual size of a rendered file, as an application would.
    png = svg2png(SQUARE, output_width=37.5, write_to=None)
    width, height, _ = border_alphas(png)
    print(f'requested 37.5 px wide, wrote a {width}x{height} PNG')
    assert (width, height) == (38, 38)

    # Invalid sizes are rejected instead of producing an empty image.
    try:
        svg2png(SQUARE, output_width=0)
    except ValueError as exception:
        print(f'invalid size rejected: {exception}')
    else:  # pragma: no cover
        raise AssertionError('output_width=0 should be rejected')


if __name__ == '__main__':
    main()
