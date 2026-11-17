#!/usr/bin/env python
"""Regenerate the state-isolation comparison images in this directory.

Run from the repository root with the project virtualenv, e.g.:

    .venv/bin/python isolation_pngs/generate.py

Every ``scenario_*`` image must be pixel-identical to its ``reference_*``
counterpart; ``pattern_siblings.png`` must show three identical solid blue
squares (the first one at 50% opacity) and ``pattern_bbox.png`` two
identical halves.

"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import cairosvg  # noqa: E402
from cairosvg.test_state_isolation import (  # noqa: E402
    BASELINE, EMPTY_LAYER_DOC, FAILING_DOC, NEXT_DOC, PATTERN_BBOX_DOC,
    PATTERN_DOC, draw_documents)

OUT = Path(__file__).resolve().parent


def save(name, png_bytes):
    (OUT / name).write_bytes(png_bytes)
    print('wrote', name)


# Baseline: four green squares, no special effects
save('reference_baseline.png', cairosvg.svg2png(BASELINE))

# Same squares preceded by clip/transform/pattern/mask/opacity layers:
# the squares must be untouched (see test_special_layers_leave_no_residue)
save('scenario_empty_layer.png', cairosvg.svg2png(EMPTY_LAYER_DOC))

# A layer with clip + transform + opacity group fails halfway (corrupt
# raster asset); the following layers and the next image are drawn through
# the same reused entry and must match the baseline exactly
png, errors, _ = draw_documents([FAILING_DOC, NEXT_DOC])
assert len(errors) == 1, errors
save('scenario_after_failed_layer_reused_entry.png', png)

# Three siblings sharing one pattern: the 50% fill-opacity of the first
# one must not leak into the shared definition
save('pattern_siblings.png', cairosvg.svg2png(PATTERN_DOC))

# Two siblings sharing an objectBoundingBox pattern: both halves identical
save('pattern_bbox.png', cairosvg.svg2png(PATTERN_BBOX_DOC))
