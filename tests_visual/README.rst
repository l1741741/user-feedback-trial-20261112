# Rendering-state isolation evidence

Real-Cairo verification scripts (no mocks of ``save``/``restore``):

- ``generate_comparison.py`` renders legend documents that exercise every
  special state (transform, clip, mask, filter operator, transparency
  group, pattern source, empty layers) followed by a plain baseline layer,
  then compares the baseline region pixel-by-pixel with the same document
  rendered alone. It also renders a document whose drawing raises a real
  PNG error and confirms the next entry-point call reproduces the baseline.
  Outputs go to ``output/`` (git-ignored), including a contact sheet.
- ``lifecycle_evidence.py`` runs the same lifecycle probes against a
  pristine checkout and the fixed tree, printing a side-by-side table.

Run (paths from the repository root)::

    export DYLD_FALLBACK_LIBRARY_PATH=<cairo lib dir>
    .venv/bin/python -I tests_visual/generate_comparison.py tests_visual/output
    git archive HEAD | tar -x -C /tmp/cairosvg-pristine
    .venv/bin/python -I tests_visual/lifecycle_evidence.py \
        /tmp/cairosvg-pristine tests_visual/output/lifecycle_evidence.txt

The automated pixel- and lifecycle-level checks live in
``cairosvg/test_state_isolation.py`` and run with the rest of the package
tests (``python -m pytest cairosvg/``).
