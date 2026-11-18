Text chunk layout and anchor regression tests
=============================================

These tests cover the SVG 1.1 text layout behavior (sections 10.5 and
10.9.2) that the rewritten ``cairosvg/text.py`` chunk engine implements:

* an explicit ``x``/``y`` opens a new, independent text chunk;
* ``text-anchor`` (start / middle / end) aligns the *whole* chunk rather
  than being re-applied to every glyph;
* supplemental ``dx``/``dy`` displacements move the single current text
  position exactly once, persist for following glyphs per the spec, and are
  never re-applied through nested or sibling ``tspan`` inheritance;
* per-glyph ``rotate`` is retained and does not leak into a later ``text``;
* separate ``text`` nodes start from independent state, so ordering or prior
  text cannot change a node's result;
* empty ``text`` nodes and repeated (byte-for-byte stable) exports behave.

Font condition
--------------

The rendered (pixel) tests use **Helvetica**, which ships with macOS and is
resolved by the cairo "toy" font API through fontconfig on Linux. When the
family is unavailable the tests are skipped (not failed) via the
``requires_font`` marker. The geometry-level tests build a ``TextState``
directly and assert numeric pen/extent positions, so they exercise the layout
logic independent of rasterization.

A tolerance of two pixels (``TOLERANCE``) accommodates font hinting and
anti-aliasing; no exact pixel matching or pre-rendered bitmap is used, and no
test inserts spaces or other content to force a visual result.

Run::

  pytest cairosvg/test_text_layout -q
