# objectBoundingBox geometry regression suite

This suite guards the geometry bounding-box computation in
`cairosvg/bounding_box.py`, which backs the `objectBoundingBox` coordinate
unit used by gradients, patterns and `clipPath`s.

## What was wrong

The old path bounding box only inspected path endpoints and control points:

* cubic (`C/c`) and quadratic (`Q/q`) Beziers were bounded by their control
  polygon, which can overshoot the true curve by a large margin — the gradient
  or pattern derived from that box was then stretched or offset;
* smooth curves (`S/s`, `T/t`) ignored the control point reflected from the
  previous segment, so the box missed a whole part of the curve;
* multiple subpaths, relative coordinates and `Z/z` close handling were not
  tracked with explicit subpath state.

Elliptical arcs already used an analytic endpoint-to-center conversion; that
math is preserved (a 3000-case fuzz comparison against the old implementation
reports a zero difference) and re-tested here.

## Layout

* `geometry.py` — an **independent** implementation: its own SVG path-data
  tokenizer, analytic Bezier/arc extrema, plus a dense-flattening cross-check.
  It imports neither Cairo nor CairoSVG.
* `pixels.py` — PNG pixel helpers built on the real `cairosvg.svg2png` entry
  point.
* `test_geometry.py` — checks the production box against the analytic
  reference (to floating-point precision) and the flattened reference, across
  ~40 paths covering every command, relative coordinates, negative
  coordinates, multiple subpaths, close paths and degenerate shapes.
* `test_rendering.py` — end-to-end PNG checks: an `objectBoundingBox` effect
  must match an equivalent `userSpaceOnUse` definition; covers linear and
  radial gradients, patterns, clip paths, transforms and definition reuse.
* `test_images.py` — guards the committed images against drift.
* `generate_images.py` — regenerates the SVG/PNG pairs in `images/`.
* `images/` — small, human-inspectable regression pictures and their sources.

## Running

From the repository root (the Cairo shared library must be loadable):

```bash
export DYLD_FALLBACK_LIBRARY_PATH="<path-to-cairo-libs>"
.venv/bin/python -m pytest -q tests/object_bounds cairosvg/test_api.py
```

Regenerating the images after an intentional rendering change:

```bash
.venv/bin/python -I tests/object_bounds/generate_images.py
```

The `-I` flag runs isolated so no stray module in the working directory can be
imported. PNG output is deterministic for a given Cairo version, so
`test_images.py` compares a fresh render byte-for-byte against the committed
PNG.

## Geometry box vs stroke box

The boxes tested here are *geometry* (fill) boxes in the node's local user
space. They intentionally ignore stroke width, markers, line caps and joins;
those belong to the stroke bounding box, which Cairo computes while painting.
The geometry calculation is read-only and never creates a Cairo path or
mutates drawing state.

## Shapes without an analytic box

* `text`/`tspan`/`textPath` have no pre-render geometry: their box comes from
  font metrics measured while drawing and is stored on the node
  (`text_bounding_box`). An unrendered text node therefore reports no box.
* Degenerate geometry (a zero-width or zero-height path such as a horizontal
  line, or an empty path) yields a valid but zero-area box; it cannot define
  an `objectBoundingBox` coordinate system, so gradients/patterns refuse to
  paint it and object-bounded clips collapse to an empty clip instead of
  raising a Cairo matrix error.
