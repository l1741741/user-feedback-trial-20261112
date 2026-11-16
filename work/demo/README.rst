Marker direction demo and raster verification
==============================================

Everything here is generated offline from the local CairoSVG checkout; no
reference images are shipped or compared against.

Files
-----

- ``marker_demo.svg`` — hand-written SVG exercising every marker case:
  straight lines with start reversal, polyline corner bisectors, cubic and
  quadratic Bézier tangents, arcs, zero-length segments, closed paths and
  polygons (one mid marker per vertex), explicit numeric ``orient``,
  stroke-width scaling, ``userSpaceOnUse`` units and a 180° closed seam.
- ``render_demo.py`` — renders ``marker_demo.svg`` to ``marker_demo.png``.
- ``verify_markers.py`` — renders small controlled SVGs and inspects the
  resulting PNG pixels to prove the markers point the right way, sit on the
  right vertices and scale with the right units.

Run from the repository root. The Cairo shared library must be reachable
(see ``NATIVE-RUNTIME.md``)::

    export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib"   # macOS example
    python work/demo/render_demo.py
    python work/demo/verify_markers.py

``verify_markers.py`` exits non-zero on the first failed geometric check and
prints the measured angle or size next to every check, so a regression is
diagnosed from numbers rather than from a missing-file assertion.

The marker geometry convention and the internal helper functions are
documented in ``docs-markers.rst`` at the repository root; unit and raster
regression tests live in ``cairosvg/test_markers.py``.
