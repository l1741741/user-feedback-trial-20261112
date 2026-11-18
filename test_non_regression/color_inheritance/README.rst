Color inheritance regression fixtures
=====================================

``themed_symbol_use.svg`` exercises ``currentColor`` and ``inherit``
resolution across definitions, references and instances:

* one ``<symbol>`` drawn from two themed groups (red / blue);
* one shared ``linearGradient`` with ``currentColor`` stops referenced from
  several groups and from inside another symbol;
* a plain path filled with a literal color as a control;
* a half-opacity instance and a draw-order swap to check that instances are
  independent and never pollute siblings.

Pixel-level assertions live in ``cairosvg/test_color_inheritance.py``.

Offline rendering (no network access is needed; only the local Cairo shared
library is required)::

    export DYLD_FALLBACK_LIBRARY_PATH="/Users/lichao/.cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/poppler/lib:/opt/homebrew/lib"

    # PNG output via the command line
    .venv/bin/python -m cairosvg \
        test_non_regression/color_inheritance/themed_symbol_use.svg \
        -o themed_symbol_use.png -f png -d 96

    # Or via the Python API
    .venv/bin/python -I -c "import cairosvg; \
cairosvg.svg2png(url='test_non_regression/color_inheritance/themed_symbol_use.svg', \
write_to='themed_symbol_use.png', output_width=400, output_height=200)"

    # Full regression + API test suite
    .venv/bin/python -m pytest -q cairosvg/

On Linux, replace ``DYLD_FALLBACK_LIBRARY_PATH`` with the equivalent
``LD_LIBRARY_PATH`` pointing at the directory containing ``libcairo.so.2``.

Expected result
---------------

* the two symbol instances in the first row are solid red and solid blue,
  each with a currentColor-stroked circle of the same color;
* the three gradient rectangles start from green, orange and teal
  respectively and fade to white at their right edge;
* the teal gradient reached through ``<use>`` matches a direct reference;
* the purple control rect is unchanged;
* the half-opacity red instance keeps red RGB with alpha ~128 and does not
  alter the fully opaque red instance rendered earlier;
* swapping the drawing order of the orange and green gradient rectangles
  leaves both colors unchanged.
