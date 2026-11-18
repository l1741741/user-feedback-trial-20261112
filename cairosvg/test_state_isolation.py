"""
Render-state isolation regression tests.

These tests render real SVG documents (no mocks of cairo save/restore) and
assert on actual pixels and on the lifecycle of the underlying cairo
surfaces. They cover the three call boundaries that must always clean up:

* a node that finishes normally, including transform, clip-path, opacity
  groups, filters and custom blend operators;
* an empty / invisible layer that draws nothing;
* a node whose drawing raises an exception mid-frame.

The shared definition dictionaries (markers, gradients, patterns, masks,
paths, filters, images) and the caller's parsed tree are intentionally left
untouched: the isolation relies on context frames and surface finalization,
not on resetting shared state.
"""

import io
import os
import subprocess

import cairocffi as cairo
import pytest
from PIL import Image

from . import svg2pdf, svg2png, svg2ps
from .parser import Tree
from .surface import PNGSurface, TAGS

WIDTH = HEIGHT = 240

# Plain reference document: a red square (top-left) and a blue circle
# (bottom-right), with no special effect at all.
BASELINE_SVG = f'''
<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}">
  <rect x="20" y="20" width="60" height="60" fill="red"/>
  <circle cx="180" cy="180" r="35" fill="blue"/>
</svg>'''

# Same two plain shapes, preceded by a layer that exercises every stateful
# mechanism: transform, clip-path, an opacity transparency group and a filter
# installing a non-default blend operator.
SPECIAL_SVG = f'''
<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}">
  <defs>
    <clipPath id="cp"><rect x="0" y="0" width="120" height="120"/></clipPath>
    <filter id="flt" x="0" y="0" width="1" height="1">
      <feBlend mode="multiply"/>
    </filter>
  </defs>
  <g transform="translate(10,10) rotate(15 50 50)" clip-path="url(#cp)"
     opacity="0.5" filter="url(#flt)">
    <rect x="10" y="10" width="80" height="80" fill="green"/>
  </g>
  <rect x="20" y="20" width="60" height="60" fill="red"/>
  <circle cx="180" cy="180" r="35" fill="blue"/>
</svg>'''

# Plain shapes separated by empty / invisible layers.
EMPTY_SVG = f'''
<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}">
  <rect x="20" y="20" width="60" height="60" fill="red"/>
  <g display="none"><rect width="50" height="50" fill="green"/></g>
  <g visibility="hidden"><circle cx="60" cy="60" r="20"/></g>
  <rect width="0" height="40" fill="green"/>
  <use href="#missing"/>
  <circle cx="180" cy="180" r="35" fill="blue"/>
</svg>'''

# A document with a real mid-render failure: the raster <image> carries bytes
# that are neither a PNG/JPEG nor an SVG, so PIL raises while the image
# node's cairo frame is already open.
BAD_IMAGE_SVG = f'''
<svg xmlns="http://www.w3.org/2000/svg"
     xmlns:xlink="http://www.w3.org/1999/xlink"
     width="{WIDTH}" height="{HEIGHT}">
  <rect x="10" y="10" width="100" height="100" fill="red"/>
  <image x="20" y="120" width="80" height="80"
         xlink:href="data:image/png;base64,dGhpcyBpcyBub3QgcG5nIGRhdGE="/>
  <circle cx="180" cy="180" r="35" fill="blue"/>
</svg>'''

# A document with a real mid-render failure located inside the drawing of a
# shared clip-path definition: the clip's own rect raises while the
# definition's tag is temporarily switched from 'clipPath' to 'g'.
CLIP_SVG = f'''
<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}">
  <defs>
    <clipPath id="cp">
      <rect x="0" y="0" width="120" height="120" fill="clip-trigger"/>
    </clipPath>
  </defs>
  <rect x="10" y="10" width="100" height="100" fill="red"
        clip-path="url(#cp)"/>
  <rect x="130" y="130" width="100" height="100" fill="blue"
        clip-path="url(#cp)"/>
</svg>'''


class InjectedDrawError(Exception):
    """Deterministic failure raised from inside a drawing callback."""


def render(markup):
    return Image.open(io.BytesIO(svg2png(bytestring=markup.encode()))).convert(
        'RGBA')


def differing_pixels(image_a, image_b):
    assert image_a.size == image_b.size
    pixels_a, pixels_b = image_a.load(), image_b.load()
    count = 0
    for y in range(image_a.size[1]):
        for x in range(image_a.size[0]):
            if pixels_a[x, y] != pixels_b[x, y]:
                count += 1
    return count


def save_artifact(image, name):
    """Optionally keep rendered PNGs for local visual comparison."""
    directory = os.environ.get('CAIROSVG_TEST_ARTIFACTS')
    if directory:
        os.makedirs(directory, exist_ok=True)
        image.save(os.path.join(directory, name))


def is_finished(surface):
    """Return whether cairo rejects further drawing on ``surface``.

    cairo 1.18 keeps reporting SUCCESS on an idle finished surface; the
    finished state is surfaced as SURFACE_FINISHED only on the next use.
    """
    try:
        cairo.Context(surface.cairo).paint()
        surface.cairo._check_status()
    except cairo.Error as exc:
        return exc.status == cairo.STATUS_SURFACE_FINISHED
    return False


def explode_rect_factory(trigger_fill):
    """Return a rect drawer that fails on nodes with ``trigger_fill``."""
    original_rect = TAGS['rect']

    def exploding_rect(surface, node):
        if node.get('fill') == trigger_fill:
            raise InjectedDrawError('injected mid-draw failure')
        return original_rect(surface, node)

    return original_rect, exploding_rect


# ---------------------------------------------------------------------------
# Pixel-level isolation on the normal and the empty-layer paths
# ---------------------------------------------------------------------------

def test_baseline_renders_expected_shapes():
    image = render(BASELINE_SVG)
    save_artifact(image, 'baseline.png')
    pixels = image.load()
    assert pixels[50, 50] == (255, 0, 0, 255)
    assert pixels[180, 180] == (0, 0, 255, 255)
    # An untouched pixel stays fully transparent.
    assert pixels[120, 120] == (0, 0, 0, 0)


def test_empty_layers_leave_no_trace():
    baseline = render(BASELINE_SVG)
    empty = render(EMPTY_SVG)
    save_artifact(empty, 'empty_layer.png')
    assert differing_pixels(baseline, empty) == 0


def test_special_layer_does_not_leak_into_following_nodes():
    baseline = render(BASELINE_SVG)
    special = render(SPECIAL_SVG)
    save_artifact(special, 'special_prefix.png')
    # The region outside the special group must be pixel-identical to the
    # plain baseline: no transform, clip, operator, source or group residue
    # may reach the following rect and circle nodes.
    assert differing_pixels(
        baseline.crop((120, 120, WIDTH, HEIGHT)),
        special.crop((120, 120, WIDTH, HEIGHT))) == 0


def test_special_layer_actually_paints_something():
    """Guard against a trivially green test: the effect must be visible."""
    baseline = render(BASELINE_SVG)
    special = render(SPECIAL_SVG)
    assert differing_pixels(baseline, special) > 0


# ---------------------------------------------------------------------------
# Mid-render exceptions: propagation and cairo frame balance
# ---------------------------------------------------------------------------

def test_real_drawing_error_still_propagates():
    with pytest.raises(Exception) as exc_info:
        svg2png(bytestring=BAD_IMAGE_SVG.encode())
    # The genuine PIL error must reach the caller; nothing is swallowed.
    assert 'identify' in str(exc_info.value).lower() or isinstance(
        exc_info.value, OSError)


def test_failed_construction_finalizes_native_surface():
    tree = Tree(bytestring=BAD_IMAGE_SVG.encode())

    class CapturingSurface(PNGSurface):
        def __init__(self, *args, **kwargs):
            try:
                super().__init__(*args, **kwargs)
            except InjectedDrawError:
                pass

    original_rect, exploding_rect = explode_rect_factory('red')
    TAGS['rect'] = exploding_rect
    try:
        surface = CapturingSurface(tree, io.BytesIO(), 96)
    finally:
        TAGS['rect'] = original_rect

    # The failed render must not keep a context or an open native surface.
    assert 'context' not in surface.__dict__
    assert is_finished(surface)


def test_draw_frame_balances_save_restore_and_groups_on_exception():
    tree = Tree(bytestring=BAD_IMAGE_SVG.encode())

    class KeepAliveSurface(PNGSurface):
        def __init__(self, *args, **kwargs):
            try:
                super().__init__(*args, **kwargs)
            except InjectedDrawError:
                pass

        def _abort(self):
            # Keep the cairo objects alive so the frame counters can be
            # inspected; the real constructor path is tested separately.
            pass

    original_rect, exploding_rect = explode_rect_factory('red')
    TAGS['rect'] = exploding_rect
    try:
        surface = KeepAliveSurface(tree, io.BytesIO(), 96)
    finally:
        TAGS['rect'] = original_rect

    context = surface.context
    counters = {'save': 0, 'restore': 0, 'push': 0, 'pop': 0}
    raw_save, raw_restore = context.save, context.restore
    raw_push, raw_pop = context.push_group, context.pop_group_to_source

    def count_save():
        counters['save'] += 1
        return raw_save()

    def count_restore():
        counters['restore'] += 1
        return raw_restore()

    def count_push(*args, **kwargs):
        counters['push'] += 1
        return raw_push(*args, **kwargs)

    def count_pop(*args, **kwargs):
        counters['pop'] += 1
        return raw_pop(*args, **kwargs)

    context.save, context.restore = count_save, count_restore
    context.push_group, context.pop_group_to_source = count_push, count_pop
    TAGS['rect'] = exploding_rect
    try:
        with pytest.raises(InjectedDrawError):
            surface.draw(tree)
    finally:
        TAGS['rect'] = original_rect
        context.save, context.restore = raw_save, raw_restore
        context.push_group, context.pop_group_to_source = raw_push, raw_pop

    assert counters['save'] == counters['restore']
    assert counters['push'] == counters['pop']
    # Nothing left on the stack: another restore must underflow.
    with pytest.raises(cairo.Error):
        context.restore()


def test_exception_inside_transparency_group_does_not_leave_group_open():
    markup = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}"
         height="{HEIGHT}">
      <g opacity="0.5">
        <rect x="10" y="10" width="50" height="50" fill="red"/>
      </g>
    </svg>'''
    tree = Tree(bytestring=markup.encode())

    class KeepAliveSurface(PNGSurface):
        def __init__(self, *args, **kwargs):
            try:
                super().__init__(*args, **kwargs)
            except InjectedDrawError:
                pass

        def _abort(self):
            pass

    original_rect, exploding_rect = explode_rect_factory('red')
    TAGS['rect'] = exploding_rect
    try:
        surface = KeepAliveSurface(tree, io.BytesIO(), 96)
    finally:
        TAGS['rect'] = original_rect

    # The <g> pushed a transparency group that the exception interrupted.
    # Its finally block must have popped the group and restored the frame,
    # so the next restore immediately underflows instead of unwinding a
    # group left behind on the stack.
    with pytest.raises(cairo.Error):
        surface.context.restore()


def test_clip_definition_is_restored_when_clipped_node_fails():
    tree = Tree(bytestring=CLIP_SVG.encode())

    class KeepAliveSurface(PNGSurface):
        def __init__(self, *args, **kwargs):
            try:
                super().__init__(*args, **kwargs)
            except InjectedDrawError:
                pass

        def _abort(self):
            pass

    original_rect, exploding_rect = explode_rect_factory('clip-trigger')
    TAGS['rect'] = exploding_rect
    try:
        surface = KeepAliveSurface(tree, io.BytesIO(), 96)
    finally:
        TAGS['rect'] = original_rect

    assert surface.paths['cp'].tag == 'clipPath'
    assert surface.stroke_and_fill is True


def test_textpath_failure_restores_stroke_and_fill_flag():
    # The referenced path is drawn with fill/stroke suppressed; an exception
    # during that very draw must not leave stroke_and_fill == False behind.
    markup = f'''
    <svg xmlns="http://www.w3.org/2000/svg"
         xmlns:xlink="http://www.w3.org/1999/xlink"
         width="{WIDTH}" height="{HEIGHT}">
      <defs>
        <path id="tp" d="M10 10 L120 120"/>
      </defs>
      <text font-size="20">
        <textPath xlink:href="#tp" href="#tp">label</textPath>
      </text>
    </svg>'''
    tree = Tree(bytestring=markup.encode())

    class KeepAliveSurface(PNGSurface):
        def __init__(self, *args, **kwargs):
            try:
                super().__init__(*args, **kwargs)
            except InjectedDrawError:
                pass

        def _abort(self):
            pass

    from .surface import TAGS as ALL_TAGS
    original_path = ALL_TAGS['path']

    def exploding_path(surface, node):
        # The textPath reference path is drawn inline with fill/stroke off.
        if node.get('id') == 'tp':
            raise InjectedDrawError('injected reference-path failure')
        return original_path(surface, node)

    ALL_TAGS['path'] = exploding_path
    try:
        surface = KeepAliveSurface(tree, io.BytesIO(), 96)
    finally:
        ALL_TAGS['path'] = original_path

    assert surface.stroke_and_fill is True


def test_textpath_flag_reset_is_local_to_text_function():
    # Call text() directly (not through surface.draw) so the text module's own
    # finally block is the only thing that can restore stroke_and_fill after
    # the reference path draw fails.
    from . import text as text_module

    markup = f'''
    <svg xmlns="http://www.w3.org/2000/svg"
         xmlns:xlink="http://www.w3.org/1999/xlink"
         width="{WIDTH}" height="{HEIGHT}">
      <defs><path id="tp" d="M10 10 L120 120"/></defs>
      <text font-size="20">
        <textPath xlink:href="#tp" href="#tp">label</textPath>
      </text>
    </svg>'''
    tree = Tree(bytestring=markup.encode())

    class KeepAliveSurface(PNGSurface):
        def __init__(self, *args, **kwargs):
            try:
                super().__init__(*args, **kwargs)
            except InjectedDrawError:
                pass

        def _abort(self):
            pass

    surface = KeepAliveSurface(tree, io.BytesIO(), 96)
    # Find the textPath node and its registered reference path.
    text_node = next(
        node for node in tree.children
        if node.tag == 'text')
    textpath_node = text_node.children[0]
    reference_path = surface.paths['tp']

    def boom(_node):
        raise InjectedDrawError('reference path failure')

    real_draw = surface.draw
    surface.draw = lambda node: boom(node) if node is reference_path \
        else real_draw(node)
    surface.stroke_and_fill = True
    with pytest.raises(InjectedDrawError):
        text_module.text(surface, textpath_node)
    assert surface.stroke_and_fill is True


def test_use_restores_context_frame_on_inner_failure():
    # Call defs.use() directly so its own finally must balance the context
    # frame it saved, even when the referenced subtree fails.
    from . import defs as defs_module

    markup = f'''
    <svg xmlns="http://www.w3.org/2000/svg"
         xmlns:xlink="http://www.w3.org/1999/xlink"
         width="{WIDTH}" height="{HEIGHT}">
      <rect id="target" x="10" y="10" width="40" height="40" fill="red"/>
      <use href="#target" x="60" y="60"/>
    </svg>'''
    tree = Tree(bytestring=markup.encode())

    class KeepAliveSurface(PNGSurface):
        def __init__(self, *args, **kwargs):
            try:
                super().__init__(*args, **kwargs)
            except InjectedDrawError:
                pass

        def _abort(self):
            pass

    surface = KeepAliveSurface(tree, io.BytesIO(), 96)
    use_node = next(node for node in tree.children if node.tag == 'use')
    real_draw = surface.draw

    def draw_with_boom(node):
        # Fail when the referenced subtree (the rect carrying id="target")
        # is drawn inside use().
        if node.get('id') == 'target':
            raise InjectedDrawError('referenced subtree failure')
        return real_draw(node)

    surface.draw = draw_with_boom
    with pytest.raises(InjectedDrawError):
        defs_module.use(surface, use_node)
    # The use() frame is gone: another restore underflows immediately.
    with pytest.raises(cairo.Error):
        surface.context.restore()


def test_repeated_exports_after_failure_are_identical():
    # First provoke a real failure through the public entry point.
    with pytest.raises(Exception):
        svg2png(bytestring=BAD_IMAGE_SVG.encode())
    pristine = svg2png(bytestring=BASELINE_SVG.encode())
    for _ in range(5):
        assert svg2png(bytestring=BASELINE_SVG.encode()) == pristine


def test_special_document_export_is_deterministic():
    first = svg2png(bytestring=SPECIAL_SVG.encode())
    for _ in range(19):
        assert svg2png(bytestring=SPECIAL_SVG.encode()) == first


# ---------------------------------------------------------------------------
# Resource release and repeated-export regressions across output formats
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('converter,magic', (
    (svg2pdf, b'%PDF'),
    (svg2ps, b'%!PS'),
))
def test_failed_streaming_export_flushes_output(tmp_path, converter, magic):
    output = tmp_path / 'failed'
    with pytest.raises(Exception):
        converter(bytestring=BAD_IMAGE_SVG.encode(), write_to=str(output))
    # Even on failure the cairo surface is finished at the call boundary, so
    # any bytes already emitted form a valid document header rather than an
    # open, half-written stream.
    assert output.read_bytes().startswith(magic)


@pytest.mark.parametrize('converter', (svg2pdf, svg2ps))
def test_failed_streaming_surface_is_finalized_when_retained(converter):
    from .surface import PDFSurface, PSSurface

    base_cls = PDFSurface if converter is svg2pdf else PSSurface

    class CapturingSurface(base_cls):
        instances = []

        def __init__(self, *args, **kwargs):
            try:
                super().__init__(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                CapturingSurface.instances.append((self, exc))
                raise

    for _ in range(5):
        with pytest.raises(Exception):
            CapturingSurface(
                Tree(bytestring=BAD_IMAGE_SVG.encode()), io.BytesIO(), 96)

    assert len(CapturingSurface.instances) == 5
    for surface, _exc in CapturingSurface.instances:
        assert 'context' not in surface.__dict__
        assert is_finished(surface)


def test_no_file_descriptor_accumulation_after_failures(tmp_path):
    if not subprocess.run(
            ['which', 'lsof'], capture_output=True).returncode == 0:
        pytest.skip('lsof is not available')

    def open_fd_count():
        result = subprocess.run(
            ['lsof', '-p', str(os.getpid()), '-a', '-d', '0-999', '-F', 'f'],
            capture_output=True, text=True, check=True)
        return sum(1 for line in result.stdout.splitlines()
                   if line.startswith('f'))

    baseline = open_fd_count()
    for index in range(15):
        with pytest.raises(Exception):
            svg2pdf(
                bytestring=BAD_IMAGE_SVG.encode(),
                write_to=str(tmp_path / f'failed_{index}.pdf'))
        with pytest.raises(Exception):
            svg2ps(
                bytestring=BAD_IMAGE_SVG.encode(),
                write_to=str(tmp_path / f'failed_{index}.ps'))
    # Give any reference-counted cairo objects a deterministic collection
    # point; explicit finalization should mean nothing accumulates anyway.
    import gc
    gc.collect()
    assert open_fd_count() <= baseline


def test_repeated_successful_exports_stay_byte_identical(tmp_path):
    outputs = set()
    for _ in range(10):
        buffer = io.BytesIO()
        svg2png(bytestring=SPECIAL_SVG.encode(), write_to=buffer)
        outputs.add(buffer.getvalue())
    assert len(outputs) == 1
