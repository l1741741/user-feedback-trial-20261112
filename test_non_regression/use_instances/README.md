# `use` instance regression fixtures

`legend.svg` is a self-contained, font-free legend for a UI icon library. It
is at least 240×160 (320×200) and exercises `use` reference instances:

- the same `symbol` used at several sizes, in two theme colors
  (`#224466` and `#cc3355`);
- a visible yellow (`#ffcc00`) corner badge in every icon instance, so the
  instance bounds can be checked on the exported pixels;
- `x`/`y` combined with an instance `transform`;
- `symbol` references with only `width` or only `height`;
- a composed button `symbol` that itself references the icon symbol
  (nested `use`), reused at two sizes and two colors.

`legend.png` is the real PNG export of that fixture, generated with:

```bash
.venv/bin/python -m cairosvg \
    test_non_regression/use_instances/legend.svg \
    -o test_non_regression/use_instances/legend.png -f png
```

The pixel checks live in `cairosvg/test_use_instances.py`, which also
re-renders the fixture in a different paint order and compares the whole
image to prove that instance state is not shared. Regenerate `legend.png`
with the command above after changing `legend.svg`.
