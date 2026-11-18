Local text-layout demo images
=============================

These SVGs/PNGs demonstrate the text chunk and ``text-anchor`` fix. They are
not part of the automated suite (the executable checks live in
``cairosvg/test_text_layout``); they are a visual reference.

Font condition
--------------

All demos set ``font-family="Helvetica"``. Helvetica ships with macOS and is
also commonly available via fontconfig on Linux. Cairo's toy font API
silently substitutes a default face if the family is missing, so on a host
without Helvetica the glyph metrics differ but the *layout rules* (chunk
boundaries, anchor shifts, single-application dx/dy) remain identical.

Render them with::

  export DYLD_FALLBACK_LIBRARY_PATH="<path-to-cairo>/lib:/opt/homebrew/lib"
  .venv/bin/python -c "from cairosvg import svg2png; \
      svg2png(url='work_demo/anchor_chunks.svg', \
              write_to='work_demo/anchor_chunks.png')"

Files
-----

anchor_chunks.svg / .png
  Reference guide lines at x=150 (start), x=300 (middle) and x=450 (end),
  plus a horizontal red baseline. Shows:
  * three simple ``text`` nodes with the three anchors;
  * one ``text`` with three ``tspan`` chunks, each anchored independently;
  * a mid-line ``x`` change (``AAA`` then ``BBB``) centering only the new
    chunk on its own coordinate;
  * an anchor switch inside a line (start ``left`` / end ``right``);
  * nested ``tspan`` dx offsets applied once, sitting on the red baseline;
  * per-glyph ``rotate`` that does not leak to the following ``text``.

chunk_probe.svg / .png
  Focused probes: multi-``x`` chunks in a single node, nested dx/dy, a dy
  that must not cross into a sibling ``text``, and an empty ``text`` node
  followed by a real one.

textpath_check.svg / .png
  Sanity check that text on a path (textPath) and a centered ordinary text
  node still render after the rewrite.

Measurement helper
------------------

``render_measure.py`` renders an SVG to a PNG and prints the black-ink extents
of arbitrary rectangular regions, ignoring the grey/red guide lines and the
transparent background. A two-pixel tolerance is used when comparing against
the guide coordinates to accommodate anti-aliasing.
