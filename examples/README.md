# Dash reference sheet

`dash-patterns.svg` is a small, font-light reference sheet for
`stroke-dasharray` and `stroke-dashoffset`. Every row is a horizontal or
simple polygon line that starts at x=340, so the rows can be compared without
any reference image: lines start at the same x, the grey rules are 10 units
apart, and the bottom ruler is exactly 100 units.

What the rows cover:

- an even list and an odd list (`5 3 2`, painted as `5 3 2 5 3 2`)
- a positive offset on the odd list
- a negative offset on the odd list (`-20`), which counts through the list as
  written rather than through the repeated list
- a negative offset on an even list
- `0 12` with round caps (dots) and with butt caps (nothing)
- percentage lengths (`10% 5%`, resolved against the normalized viewport
  diagonal)
- scientific notation (`1e1 5e0`)
- an invalid list (`4 -2`), which CSS says to ignore, leaving a solid stroke
- a dashed group whose child turns dashing off: the child is solid, and the
  following sibling is solid too
- a corner where the dash phase continues around the turn
- a closed path

Regenerate the PNG and print the measured run lengths with:

```bash
.venv/bin/python examples/dash-patterns.py
```

Each printed run is `#<length>` for a painted segment and `.<length>` for a
blank one. Both CairoSVG and a browser should produce the same sequence of
runs; the visual test suite in `cairosvg/test_dash.py` asserts these numbers
on short reference lines.
