"""Probe cairo toy font resolution and metrics on this machine."""
import os
import cairocffi as cairo

SURF = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 200)
ctx = cairo.Context(SURF)
ctx.select_font_face('sans-serif')
ctx.set_font_size(20)
print('font face:', ctx.get_font_face())
print('font extents:', ctx.font_extents())
for s in ('AB', 'Hello', 'iii', 'MMM'):
    print(s, ctx.text_extents(s))
