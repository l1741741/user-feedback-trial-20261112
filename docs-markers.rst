Marker rendering
================

This document describes how CairoSVG turns the SVG ``marker-start``,
``marker-mid`` and ``marker-end`` properties (and the ``marker`` shorthand)
into painted arrowheads and how the rendering code is organized. It is meant
for maintainers; the SVG-facing behavior follows the SVG 1.1 *Markers*
chapter.

Data flow
---------

1. Geometry builders (:mod:`cairosvg.path` for ``path`` elements and
   :mod:`cairosvg.shapes` for ``line``, ``polyline`` and ``polygon``) fill
   ``node.vertices`` while tracing the shape. The deque alternates coordinate
   points ``(x, y)`` and segment angle pairs:

   ``point, (start_tangent, end_tangent), point, ...``

   Every segment type now stores *outgoing travel directions* in radians with
   the y axis pointing down (0 = east, π/2 = south). A straight line stores
   the same angle twice; curves store their two endpoint tangents and arcs
   derive their tangents from the radial direction plus or minus π/2
   depending on the sweep flag.

2. ``None`` separates two independent subpaths; the
   :data:`cairosvg.path.CLOSE_PATH` sentinel marks a closed subpath (a ``z``
   command or a ``polygon``).

3. :func:`cairosvg.path._parse_marker_vertices` turns this raw stream into
   structured placements ``(point, angle, position)`` with ``position`` in
   ``('start', 'mid', 'end')``:

   - zero-length segments are recorded as ``None`` and skipped, so a marker
     always takes its orientation from the nearest non-degenerate segment on
     each side;
   - open subpaths get one start and one end marker plus a mid marker at each
     interior corner;
   - closed subpaths get exactly one mid marker per vertex — the closing
     vertex joins the last segment back to the first and is never duplicated.

4. :func:`cairosvg.path.draw_markers` resolves the marker definitions and
   calls :func:`cairosvg.path._draw_marker` for each placement.

Orientation
-----------

- ``orient="auto"``: the marker points along the segment travel direction.
- ``orient="auto-start-reverse"``: behaves like ``auto`` except that the
  *start* marker is rotated by an extra π. Mid and end markers are unaffected.
- A numeric ``orient`` keeps that fixed angle regardless of the path.

Because a marker geometry whose tip is built on the positive local x axis
lands on the reference point at zero rotation, the Cairo rotation applied for
an automatic marker is ``π - travel_direction``; numeric orientations are
passed through unchanged. All of this conversion lives in
:func:`cairosvg.path._draw_marker`, so the geometry builders only ever deal
with plain travel directions.

Units, viewBox and reference point
----------------------------------

:func:`cairosvg.path._marker_geometry` computes, for every individual marker
instance:

- the ``markerUnits`` scale (the host stroke width for the default
  ``strokeWidth``, or 1 for ``userSpaceOnUse``);
- the viewport scale from ``markerWidth``/``markerHeight`` and the marker
  ``viewBox`` (honoring ``preserveAspectRatio``);
- the translation that puts ``(refX, refY)`` on the host vertex.

These values are recomputed from the marker node and the current host node.
Nothing is cached on the shared marker definition, so the same marker can be
referenced by paths with different stroke widths without contaminating each
other.

Helpers
-------

- :func:`cairosvg.path.quadratic_angles` — endpoint tangents of a quadratic
  Bézier.
- :func:`cairosvg.path.cubic_angles` — endpoint tangents of a cubic Bézier,
  with a fallback when an endpoint coincides with its control point.
- :func:`cairosvg.path.marker_mid_angle` — corner bisector, falling back to
  the incoming tangent when the two segments are exactly opposite.

Demos and verification
----------------------

``work/demo`` contains an offline, self-contained illustration and checks
that read real pixels instead of comparing against stored images:

- ``marker_demo.svg`` — hand-written SVG covering straight lines, polylines,
  cubic and quadratic curves, arcs, closed paths and polygons, zero-length
  segments, explicit orientation, stroke-width scaling and
  ``userSpaceOnUse`` markers.
- ``render_demo.py`` — renders the SVG to ``marker_demo.png`` with the local
  CairoSVG checkout.
- ``verify_markers.py`` — renders small controlled SVGs and asserts on the
  actual direction, position and scaling of the painted markers.

Run them from the repository root (the Cairo shared library must be
available, see ``NATIVE-RUNTIME.md``)::

    python work/demo/render_demo.py
    python work/demo/verify_markers.py
    python -m pytest cairosvg/test_markers.py
