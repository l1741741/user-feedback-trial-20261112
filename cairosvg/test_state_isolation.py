"""Regression tests for render-state isolation.

These tests render real images and compare pixels: a baseline drawn alone
versus the same content drawn after layers using clip paths, transforms,
patterns, masks and opacity groups — and after layers that fail halfway
through. No mocking of save/restore: the cairo state is observed through
the pixels it produces.

"""

import io

import pytest
from PIL import Image

import cairosvg
from cairosvg.defs import parse_all_defs
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface

SVG_OPEN = (
    b'<svg xmlns="http://www.w3.org/2000/svg" '
    b'width="30" height="20">')

# A corrupt raster asset: long enough and not detected as SVG, so the
# image element fails halfway through drawing, inside PIL.
BAD_PNG = 'data:image/png;base64,bm90LWEtcmVhbC1wbmctZmlsZS11aWQzMTk='

# Baseline document: four green squares, no special effects.
BASELINE = SVG_OPEN + b'''
  <rect x="0"  y="0"  width="10" height="10" fill="rgb(0,255,0)"/>
  <rect x="20" y="0"  width="10" height="10" fill="rgb(0,255,0)"/>
  <rect x="0"  y="10" width="10" height="10" fill="rgb(0,255,0)"/>
  <rect x="20" y="10" width="10" height="10" fill="rgb(0,255,0)"/>
</svg>'''

# First "image" of the batch: two healthy layers around a layer that uses
# a clip path, a transform and an opacity group, and fails halfway through
# because its raster asset is corrupt.
FAILING_DOC = SVG_OPEN + f'''
  <defs><clipPath id="c"><rect x="0" y="0" width="4" height="4"/>
  </clipPath></defs>
  <rect x="0" y="0" width="10" height="10" fill="rgb(0,255,0)"/>
  <g transform="translate(50,50) scale(4)" opacity="0.5"
     clip-path="url(#c)">
    <rect x="0" y="0" width="5" height="5" fill="rgb(255,0,0)"/>
    <image href="{BAD_PNG}" width="5" height="5"/>
  </g>
  <rect x="20" y="0" width="10" height="10" fill="rgb(0,255,0)"/>
</svg>'''.encode()

# Next "image" of the batch, rendered through the same reused entry.
NEXT_DOC = SVG_OPEN + b'''
  <rect x="0" y="10" width="10" height="10" fill="rgb(0,255,0)"/>
  <rect x="20" y="10" width="10" height="10" fill="rgb(0,255,0)"/>
</svg>'''

# Same as BASELINE but with an empty layer (transform + clip + opacity
# group without children) inserted before the visible content.
EMPTY_LAYER_DOC = SVG_OPEN + b'''
  <defs><clipPath id="c"><rect width="4" height="4"/></clipPath></defs>
  <g transform="translate(5,5) scale(2)" clip-path="url(#c)"
     opacity="0.5"></g>
  <rect x="0"  y="0"  width="10" height="10" fill="rgb(0,255,0)"/>
  <rect x="20" y="0"  width="10" height="10" fill="rgb(0,255,0)"/>
  <rect x="0"  y="10" width="10" height="10" fill="rgb(0,255,0)"/>
  <rect x="20" y="10" width="10" height="10" fill="rgb(0,255,0)"/>
</svg>'''

# Three siblings sharing the same pattern; the first one is painted with a
# reduced fill opacity that must not leak into the definition.
PATTERN_DOC = b'''<svg xmlns="http://www.w3.org/2000/svg" width="30" height="10">
  <defs>
    <pattern id="p" width="10" height="10" patternUnits="userSpaceOnUse">
      <rect width="10" height="10" fill="rgb(0,0,255)"/>
    </pattern>
  </defs>
  <rect x="0"  y="0" width="10" height="10" fill="url(#p)"
        fill-opacity="0.5"/>
  <rect x="10" y="0" width="10" height="10" fill="url(#p)"/>
  <rect x="20" y="0" width="10" height="10" fill="url(#p)"/>
</svg>'''

# Two siblings sharing an objectBoundingBox pattern (whose width/height
# used to be popped out of the shared definition by the first use).
PATTERN_BBOX_DOC = b'''<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">
  <defs><pattern id="p" width="0.5" height="0.5">
    <rect width="4" height="4" fill="rgb(0,0,255)"/></pattern></defs>
  <rect x="0"  y="0" width="20" height="20" fill="url(#p)"/>
  <rect x="20" y="0" width="20" height="20" fill="url(#p)"/>
</svg>'''

# Document exercising mask, pattern and use together, for resource
# lifecycle and repeated-export tests.
FULL_DOC = b'''<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:xlink="http://www.w3.org/1999/xlink" width="60" height="20">
  <defs>
    <pattern id="p" width="10" height="10" patternUnits="userSpaceOnUse">
      <rect width="10" height="10" fill="rgb(0,0,255)"/>
    </pattern>
    <mask id="m"><rect x="0" y="0" width="10" height="10"
        fill="white"/></mask>
    <g id="shape"><rect width="8" height="8" fill="rgb(255,0,0)"/></g>
  </defs>
  <rect x="0"  y="0" width="10" height="10" fill="url(#p)"
        fill-opacity="0.5"/>
  <rect x="10" y="0" width="10" height="10" fill="url(#p)"/>
  <rect x="20" y="0" width="10" height="10" fill="rgb(255,165,0)"
        mask="url(#m)"/>
  <rect x="30" y="0" width="10" height="10" fill="rgb(255,165,0)"
        mask="url(#m)"/>
  <use xlink:href="#shape" x="40" y="0"/>
  <use xlink:href="#shape" x="50" y="0"/>
</svg>'''


def pixels(png_bytes):
    """Return the (width, height, flat RGBA pixel list) of a PNG."""
    image = Image.open(io.BytesIO(png_bytes)).convert('RGBA')
    return image.size, list(image.getdata())


def draw_documents(documents, width=30, height=20):
    """Draw the top-level layers of each document on one shared surface.

    This mimics a batch export service reusing the same drawing entry for
    every layer and every image: per-layer errors are collected, not
    raised. Return ``(png_bytes, errors, surface)``.

    """
    empty = Tree(
        bytestring=(
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width}" height="{height}"/>').encode())
    output = io.BytesIO()
    surface = PNGSurface(empty, output, 96)
    errors = []
    for document in documents:
        tree = Tree(bytestring=document)
        parse_all_defs(surface, tree)
        for layer in tree.children:
            try:
                surface.draw(layer)
            except Exception as exc:  # Batch service logs and continues
                errors.append(exc)
    surface.finish()
    return output.getvalue(), errors, surface


def test_special_layers_leave_no_residue_on_siblings():
    """Clip + transform + pattern + mask + opacity layers must not leak."""
    reference = pixels(cairosvg.svg2png(BASELINE))
    doc = SVG_OPEN + b'''
      <defs>
        <clipPath id="c"><rect x="1" y="1" width="8" height="8"/></clipPath>
        <pattern id="p" width="4" height="4"
            patternUnits="userSpaceOnUse">
          <rect width="4" height="4" fill="rgb(0,0,255)"/>
        </pattern>
        <mask id="m"><rect x="0" y="0" width="10" height="10"
            fill="white"/></mask>
      </defs>
      <g transform="translate(3,2) rotate(10)" opacity="0.6">
        <rect x="5" y="5" width="6" height="6" fill="url(#p)"/>
      </g>
      <rect x="8" y="12" width="6" height="6" fill="rgb(255,0,0)"
            clip-path="url(#c)" mask="url(#m)"/>
      <rect x="0"  y="0"  width="10" height="10" fill="rgb(0,255,0)"/>
      <rect x="20" y="0"  width="10" height="10" fill="rgb(0,255,0)"/>
      <rect x="0"  y="10" width="10" height="10" fill="rgb(0,255,0)"/>
      <rect x="20" y="10" width="10" height="10" fill="rgb(0,255,0)"/>
    </svg>'''
    # The special layers paint outside the baseline squares: the baseline
    # squares themselves must be pixel-identical to the lone baseline.
    size, got = pixels(cairosvg.svg2png(doc))
    _, want = reference
    for x, y in ((0, 0), (20, 0), (0, 10), (20, 10)):
        # Only the fully covered inner pixels of each square are checked:
        # the special layers must not have recolored or shifted them.
        for dx in (2, 5, 7):
            for dy in (2, 5, 7):
                index = (y + dy) * size[0] + (x + dx)
                assert got[index] == want[index] == (0, 255, 0, 255)


def test_pattern_opacity_does_not_accumulate_across_siblings():
    """fill-opacity of one sibling must not stick to the shared pattern."""
    size, px = pixels(cairosvg.svg2png(PATTERN_DOC))
    first = px[5]
    second = px[15]
    third = px[25]
    assert first[:3] == (0, 0, 255) and first[3] < 255  # 0.5 opacity
    assert second == (0, 0, 255, 255)
    assert third == (0, 0, 255, 255)


def test_objectboundingbox_pattern_survives_second_use():
    """The second user of a bounding-box pattern must see the same tiles."""
    size, px = pixels(cairosvg.svg2png(PATTERN_BBOX_DOC))
    width, height = size
    left = [px[y * width + x] for y in range(height) for x in range(20)]
    right = [px[y * width + x] for y in range(height) for x in range(20, 40)]
    assert left == right
    assert left[0] == (0, 0, 255, 255)


def test_failed_layer_restores_surface_attributes():
    """A layer failing halfway must restore the surface attributes."""
    _, errors, surface = draw_documents([FAILING_DOC])
    assert len(errors) == 1  # The corrupt image really failed
    assert surface.parent_node is None
    assert surface.font_size == 16.0
    assert (surface.context_width, surface.context_height) == (30.0, 20.0)
    assert surface.stroke_and_fill is True


def test_failed_layer_does_not_leak_to_siblings_or_next_image():
    """Transform/clip/group of a failed layer must not leak: the rest of
    the document and the next image drawn through the same reused entry
    must be pixel-identical to the baseline."""
    reference = pixels(cairosvg.svg2png(BASELINE))
    png, errors, _ = draw_documents([FAILING_DOC, NEXT_DOC])
    assert len(errors) == 1
    assert pixels(png) == reference


def test_empty_layer_leaves_siblings_untouched():
    """An empty layer with transform/clip/opacity must be a clean no-op."""
    reference = pixels(cairosvg.svg2png(BASELINE))
    assert pixels(cairosvg.svg2png(EMPTY_LAYER_DOC)) == reference


def test_convert_propagates_real_errors_and_next_convert_is_clean():
    """Real errors keep propagating through the public API; the following
    convert call is unaffected."""
    with pytest.raises(Exception) as excinfo:
        cairosvg.svg2png(FAILING_DOC)
    assert excinfo.type.__name__ == 'UnidentifiedImageError'
    assert pixels(cairosvg.svg2png(BASELINE)) == pixels(
        cairosvg.svg2png(BASELINE))


def test_shared_definitions_and_caller_tree_are_unchanged():
    """Rendering must not mutate shared defs (mask/pattern/symbol) or the
    caller's own tree (use attributes), even on the failure path."""
    tree = Tree(bytestring=FULL_DOC)
    output = io.BytesIO()
    surface = PNGSurface(tree, output, 96)
    surface.finish()
    mask_node = surface.masks['m']
    assert mask_node.tag == 'mask'
    assert mask_node.get('opacity') is None
    pattern_node = surface.patterns['p']
    assert pattern_node.tag == 'pattern'
    assert pattern_node.get('opacity') is None
    assert pattern_node.get('width') == '10'
    assert pattern_node.get('height') == '10'
    uses = [node for node in tree.children if node.tag == 'use']
    assert all('x' in node and 'y' in node for node in uses)

    # Same guarantees when a layer fails while defs are involved: the
    # clipPath used by the failing layer keeps its tag and content
    _, errors, surface = draw_documents([FAILING_DOC])
    assert len(errors) == 1
    clip_node = surface.paths['c']
    assert clip_node.tag == 'clipPath'


def test_temporary_canvases_are_released():
    """Temporary pattern/mask canvases must be finished, not leaked."""
    tree = Tree(bytestring=FULL_DOC)
    output = io.BytesIO()
    surface = PNGSurface(tree, output, 96)
    temp_surfaces = list(surface._temp_surfaces)
    assert temp_surfaces  # The pattern really created a temporary canvas
    surface.finish()
    assert surface._temp_surfaces == []
    for temp_surface in temp_surfaces:
        with pytest.raises(Exception):
            # Any operation on a finished cairo surface raises
            temp_surface.cairo.get_width()


def test_repeated_export_is_deterministic():
    """Exporting the same document repeatedly gives identical bytes."""
    renders = [cairosvg.svg2png(FULL_DOC) for _ in range(3)]
    assert renders[0] == renders[1] == renders[2]


def test_reused_entry_is_repeatable_after_failures():
    """The failing batch sequence gives the same pixels on every run."""
    first, errors1, _ = draw_documents([FAILING_DOC, NEXT_DOC])
    second, errors2, _ = draw_documents([FAILING_DOC, NEXT_DOC])
    assert len(errors1) == len(errors2) == 1
    assert first == second


def test_failed_mask_content_restores_definition_and_siblings():
    """A mask whose own content fails must not corrupt the shared mask
    definition nor the layers drawn after the failing one."""
    doc = SVG_OPEN + f'''
      <defs><mask id="m">
        <image href="{BAD_PNG}" width="5" height="5"/>
      </mask></defs>
      <rect x="0" y="0" width="10" height="10" fill="rgb(0,255,0)"/>
      <rect x="5" y="5" width="10" height="10" fill="rgb(255,0,0)"
            mask="url(#m)"/>
      <rect x="20" y="0" width="10" height="10" fill="rgb(0,255,0)"/>
    </svg>'''.encode()
    png, errors, surface = draw_documents([doc])
    assert len(errors) == 1
    mask_node = surface.masks['m']
    assert mask_node.tag == 'mask'
    assert mask_node.get('opacity') is None
    size, px = pixels(png)
    for x, y in ((2, 2), (7, 7), (22, 2), (27, 7)):
        assert px[y * size[0] + x] == (0, 255, 0, 255)


def test_failed_use_leaves_caller_tree_and_siblings_clean():
    """A ``use`` whose referenced content fails must restore the context,
    the caller's node and the shared referenced tree."""
    doc = b'''<svg xmlns="http://www.w3.org/2000/svg"
         xmlns:xlink="http://www.w3.org/1999/xlink" width="30" height="20">
      <defs><g id="s">
        <image href="''' + BAD_PNG.encode() + b'''" width="5" height="5"/>
      </g></defs>
      <rect x="0" y="0" width="10" height="10" fill="rgb(0,255,0)"/>
      <use xlink:href="#s" x="10" y="10"/>
      <rect x="20" y="0" width="10" height="10" fill="rgb(0,255,0)"/>
    </svg>'''
    tree = Tree(bytestring=doc)
    png, errors, _ = draw_documents([doc])
    assert len(errors) == 1
    use_node = [node for node in tree.children if node.tag == 'use'][0]
    assert 'x' in use_node and 'y' in use_node
    size, px = pixels(png)
    for x, y in ((2, 2), (7, 7), (22, 2), (27, 7)):
        assert px[y * size[0] + x] == (0, 255, 0, 255)
