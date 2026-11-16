Output size and viewport
========================

CairoSVG renders a document at the size you ask for. The size can come from
the document itself (``width``/``height`` attributes, possibly in physical
units), from the ``scale`` value, or from ``output_width``/``output_height``.
This page explains how those inputs combine, and what the PNG surface really
writes.

Choosing the output size
------------------------

Given a document whose own size in pixels is ``(width, height)`` (after
converting physical units with ``dpi``, and after resolving percentages
against ``parent_width``/``parent_height``):

* no ``output_width`` and no ``output_height``: the size is
  ``(width * scale, height * scale)``;
* only ``output_width``: the height is deduced from the document's aspect
  ratio, ``height * output_width / width``;
* only ``output_height``: the width is deduced from the aspect ratio,
  ``width * output_height / height``;
* both: they are used as-is, even when the requested ratio differs from the
  document's. The document then fits the requested viewport according to its
  ``preserveAspectRatio`` value (``xMidYMid meet`` by default, which
  letterboxes it).

In every case the value must be finite and strictly positive. Anything else,
including ``NaN``, infinity, zero, a negative number, or a value that rounds
to zero pixels, raises ``ValueError('The SVG size is undefined')``. The check
happens *before* the output surface is created, so a rejected conversion never
leaves a truncated or empty output file behind.

Raster rounding
---------------

The PNG surface is a grid of whole pixels, so it creates
``int(round(size))``-sized images (Python's ``round`` rounds halves to the
even integer). Vector formats (PDF, PS, EPS, SVG) keep the exact requested
size instead.

Because the rounded surface can be slightly larger or smaller than the
requested size, the root viewport is stretched onto the rounded grid. For a
request at 37.5 px the surface is 38 px wide, and the whole 37.5-user-unit
viewport is mapped onto those 38 columns. This is what makes the output
consistent:

* the content covers the whole surface, so there is no stray transparent row
  or column on the right and bottom edges;
* percentage and other viewport-relative lengths are resolved against the
  same size that the file really has;
* ``viewBox`` and ``preserveAspectRatio`` still apply as usual, so a non-zero
  ``viewBox`` origin is not shifted, and a letterboxed document keeps its
  margins.

The result is deterministic: rendering the same input with the same options
twice gives byte-identical output.

Run the example
---------------

``docs/output_size_example.py`` renders a full-bleed 1 cm square with a few
size options and prints the actual PNG size and border alphas:

.. code-block:: bash

    python docs/output_size_example.py

The corresponding tests live in ``cairosvg/test_output_size.py`` and check
exact pixel coordinates for units, ``dpi``, fractional and very small sizes,
non-zero ``viewBox`` origins, transparent backgrounds and repeated calls.