# Stroke joins, line caps and miter limit — defect analysis and verification

All commands run offline against the native Cairo shared library. On this
machine the library path is supplied through `DYLD_FALLBACK_LIBRARY_PATH`.

```bash
export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"
.venv/bin/python -m pytest -q cairosvg/
```

## Defects located in the pinned source

### 1. Invalid `stroke-miterlimit` aborted the whole render

`surface.py` called `float(node.get('stroke-miterlimit', 4))` directly. Any
non-numeric, non-finite (`nan`/`inf`) or out-of-range value raised
`ValueError: could not convert string to float`, the exception propagated out
of `Surface.draw`, and **no PNG was produced**. Every sibling element in the
document was lost and the drawing context never reached a clean state.

### 2. Explicit `miter` join / `butt` cap could not reset an inherited value

The cap/join code only called `set_line_cap` / `set_line_join` for `round`,
`square` and `bevel`. The SVG initial keywords `butt` (cap) and `miter`
(join) had **no branch**, so an element that inherited `round` from an
ancestor but declared `stroke-linejoin="miter"` (or `stroke-linecap="butt"`)
kept the inherited round style. This is the "after changing linejoin the
visible boundary is wrong / clipped" symptom: an author asking for a sharp
miter or a flat butt still got a rounded outline.

### 3. Unknown keywords silently left the context in the inherited state

An unrecognised `stroke-linejoin` / `stroke-linecap` value did nothing, so the
result depended on whatever an ancestor had set instead of falling back to the
defined initial value (`miter` / `butt`).

### 4. Quadratic Bézier segments hard-coded a zero tangent angle

In `path.py`, every `q/Q/t/T` segment appended `(0, 0)` as its tangent angles,
whereas cubic `c/C/s/S` computed the real tangents. Markers attached at a
quadratic vertex were therefore rotated to a fixed horizontal orientation
instead of following the curve tangent (see
`png/quadratic_marker_original.png` vs `png/quadratic_marker_fixed.png`).

## What the fix changes

* `cairosvg/helpers.py`
  * `STROKE_LINE_CAP` / `STROKE_LINE_JOIN` tables covering **all** legal SVG
    keywords, including the initial `butt` and `miter`.
  * `stroke_line_cap()` / `stroke_line_join()` map a keyword to the Cairo
    enum and fall back to the SVG initial value for missing/unknown input.
  * `stroke_miter_limit()` parses defensively: missing, unparsable,
    non-finite or `< 1` values fall back to the SVG default `4`, so an
    invalid document never raises or poisons the Cairo context.
  * `quadratic_angles()` computes the real start/end tangent angles of a
    quadratic Bézier, with a chord fallback for degenerate (coincident)
    control points so zero-length tangents never collapse back to zero.
* `cairosvg/surface.py` sets cap, join and miter limit **unconditionally** for
  every node through those helpers.
* `cairosvg/path.py` uses `quadratic_angles()` for all four quadratic
  commands (`q`, `Q`, `t`, `T`).

## Scope explicitly verified (and what is intentionally left to Cairo)

| Requirement | How it is covered |
| --- | --- |
| Line, quadratic and cubic segments and closed contours honour the chosen join | `test_join_applies_to_mixed_segments_and_closed_contour`, plus `png/mixed_closed_*` and `png/sharp_angle_*`. Cairo applies one join style to every vertex of a path regardless of segment type, including the `Z` close vertex. |
| Invalid values use existing compatibility handling, context never errors | `test_stroke_miter_limit_fallback`, `test_invalid_miterlimit_*`, `test_stroke_line_*_mapping`. Invalid input falls back to the SVG initial/default value instead of raising. |
| Transforms and object bounds match the actual stroke | Stroking happens on the transformed Cairo path, so line width, joins and miters all move with the CTM (`test_*` render checks; manual rotated-group check shows the ink bounding box rotating with the group). Geometry bounding boxes remain fill/geometry boxes by design (they feed `objectBoundingBox` gradients/patterns and marker sizing), unchanged. |
| Reusing a path does not mutate the original node | `test_reused_path_does_not_mutate_original_node` captures the rebuilt `vertices` for three `<use>` references: same node id, identical vertex list each time, original `d` untouched. `test_reused_path_keeps_its_markers_and_geometry` counts four marker blobs for two uses. |
| Short and zero-length segments | `test_short_and_zero_length_segments_render` and `png/zero_length_*`. Degenerate `L x y L x y` sub-paths are skipped cleanly by Cairo while the real segments and their caps still draw. |
| Miter spike on a sharp acute angle | `test_miterlimit_clamps_the_spike_on_a_sharp_angle` and `png/sharp_angle_*`: a large limit keeps the spike, `miterlimit="1"` converts the join to a bevel and removes it. |
| End points (caps) and joins visible, not clipped | `test_explicit_butt_resets_inherited_round_cap`, `test_explicit_miter_resets_inherited_round_join`, `png/join_reset_*`. |

The geometric stroke itself (miter geometry, bevel, round join, cap shape,
dash handling, degenerate-segment behaviour) is performed by Cairo and was
already correct; the CairoSVG defects were in attribute validation, keyword
coverage and the quadratic tangent bookkeeping.

## Tests

* `cairosvg/test_stroke.py` — 35 offline checks, a mix of pure helper tests
  and end-to-end PNG tests that inspect the rendered pixels (ink bounding
  boxes, column tops to distinguish miter from round, connected-component
  counting for markers). Against the pristine pinned source 23 of them fail;
  with the fix all pass.
* `cairosvg/test_api.py` — the original suite still passes. Combined run:
  **42 passed**.
* Documents that omit these attributes render **byte-identical** PNGs before
  and after the change (verified against a pristine `HEAD` materialised in
  the scratch directory), so there is no default-behaviour regression.

## Regenerating the comparison images

`make_comparison.py` materialises the pristine package from `git HEAD`,
renders the fixtures with both trees and writes side-by-side PNGs:

```bash
.venv/bin/python -I test_stroke_artifacts/make_comparison.py
```

Outputs land in `test_stroke_artifacts/png/`. The generated pristine copy and
other scratch work live under ignored `_scratch/` / top-level `work/`.
