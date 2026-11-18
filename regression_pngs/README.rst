# Filter regression PNGs

These PNGs are rendered from the small, font-free, pure-color SVG fixtures
embedded in `cairosvg/test_filter.py` (one file per test).

`cairosvg/test_filter.py` checks exact RGBA values at interior and exterior
pixels chosen by analytic geometry, so antialiased edges are never comparison
points. The PNGs in this folder are reference renders of the same fixtures:
`test_filter_regression_pngs` re-renders each fixture and requires the output
to be byte-identical, catching any change to the filter pipeline that the
pixel probes might miss.

Regenerate them after an intentional rendering change with the fixtures
defined in ``REGRESSION_FIXTURES`` (``cairosvg/test_filter.py``)::

    .venv/bin/python -c "from cairosvg.test_filter import (
        REGRESSION_FIXTURES, REGRESSION_FOLDER);
    from cairosvg import svg2png
    [ (REGRESSION_FOLDER / (n + '.png')).write_bytes(
        svg2png(REGRESSION_FIXTURES[n].encode())) for n in REGRESSION_FIXTURES ]"

or simply run the checks directly:

```bash
.venv/bin/python -m pytest -q cairosvg/test_filter.py
```

Coverage of the reference images:

- `test_offset_moves_only_the_filtered_shape` — feOffset shifts only the
  filtered shape; the unfiltered sibling keeps its place.
- `test_offset_follows_host_scale_once` — a `scale(2)` host applies once.
- `test_object_bounding_box_offset_is_per_axis` — a non-square box uses two
  different scales for `dx` and `dy`.
- `test_filter_region_is_clipped` — the filter region clips the result.
- `test_region_clips_the_result_not_the_source` — an offset brings an
  out-of-region source in, and the region clips the result.
- `test_semi_transparent_shadow_keeps_its_alpha` — the shadow keeps its 50%
  alpha and the source stays on top.
- `test_two_filtered_siblings_are_independent` — two filtered siblings each
  get their own group.
- `test_nested_filters_stack_offsets` — a filtered group containing a
  filtered child composes the two offsets.
- `test_no_filter_sibling_is_painted_after_filtered_shape` — no filter state
  leaks into the following layer.
- `test_use_element_with_filter_is_shifted_once` — a filtered `use` reference
  is shifted once by its own `x`.
- `test_empty_filter_draws_nothing` — an empty filter hides only its own
  element.
