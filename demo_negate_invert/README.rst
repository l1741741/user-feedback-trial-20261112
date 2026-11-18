=========================
 Negate / invert demo
=========================

This folder demonstrates the two independent conversion options
``negate_colors`` and ``invert_images`` on a single SVG layout that mixes

* vector fills, strokes, gradients, patterns and text,
* a semi-transparent RGBA PNG,
* a palette PNG with a transparent index,
* an RGB PNG using a ``tRNS`` colour key,
* a JPEG **and** a PNG carrying EXIF orientation 6,
* a CMYK TIFF,
* a 16-bit grayscale PNG,
* the same image reused once inside a ``clipPath`` and once scaled.

The layout is 480×320 (the task asked for at least 240×160), so the exported
PNGs are large enough to inspect by eye.

Files
=====

``make_assets.py``
    Writes the raster assets to ``assets/``.  Run it once; the files are
    deterministic and no fixture has to be committed.  ``demo.py`` calls it
    automatically when the assets are missing.

``layout.svg``
    The 480×320 demo layout referencing ``assets/``.

``demo.py``
    Generates ``assets/`` if needed, exports ``output/normal.png``,
    ``output/negate-only.png``, ``output/invert-only.png`` and
    ``output/both.png``, then checks the actual pixels and exits non-zero if
    anything is wrong.

Run it
======

The Cairo shared library must be reachable (see ``NATIVE-RUNTIME.md``)::

    export DYLD_FALLBACK_LIBRARY_PATH="/path/to/lib:/opt/homebrew/lib"
    python demo_negate_invert/demo.py

What the options mean
=====================

===========================  ======================  ======================
option                        affects                 does not affect
===========================  ======================  ======================
``negate_colors=True``        vector fill, stroke,    raster pixels
                              gradient stops,         alpha channels
                              pattern colors, text
``invert_images=True``        raster pixels           vector colors
                              (RGB channels only)     alpha channels
===========================  ======================  ======================

The options are independent:

* ``negate_colors`` and ``invert_images`` are only applied where a color is
  taken from the SVG (vectors) or from a decoded image (rasters);
* each is applied **once**, so enabling both never inverts the same pixel
  twice;
* transparent pixels stay transparent in every combination, transparent
  images never paint a black background;
* EXIF orientation is applied before inversion, so the geometry of an image
  is the same whether ``invert_images`` is on or off;
* with both options off, the output is byte-identical to a default call;
* repeated conversions do not modify the caller’s image data and do not
  change later exports.

Masks describe transparency rather than displayed colors, so mask luminance
is deliberately *not* negated by ``negate_colors``; patterns, on the other
hand, are visible artwork and do follow the option.

Unsupported image modes
=======================

``invert_images`` needs an 8-bit colour interpretation.  Grayscale
(including 16-bit, scaled rather than clamped), palette, ``RGB``, ``RGBA``,
``CMYK``, ``LA`` and premultiplied ``RGBa``/``PA``/``La`` images are
supported.  Float, ``HSV``, ``LAB`` and ``YCbCr`` images raise a
``ValueError`` with an explicit message instead of being silently converted
to wrong colors.

Image access
============

``demo.py`` reads images through a ``url_fetcher`` that only resolves files
inside this demo’s ``assets`` folder; it never passes ``unsafe=True``.
Nothing here weakens CairoSVG’s default restriction on external resources.
