"""Check whether cairo text_path survives save/restore and fill_preserve."""
import cairocffi as cairo

surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 200, 100)
ctx = cairo.Context(surf)
ctx.select_font_face('Helvetica')
ctx.set_font_size(20)

# mimic new engine: glyph path inside save/restore
ctx.save()
ctx.move_to(10, 40)
ctx.text_path('AB')
ctx.restore()
print('has path after restore:', ctx.has_current_point(),
      'path extents:', ctx.fill_extents())

ctx.save()
ctx.set_source_rgb(0, 0, 0)
ctx.fill_preserve()
ctx.restore()
print('path still present after fill_preserve:', ctx.has_current_point())
ctx.set_line_width(1)
ctx.stroke()
print('stroke ok, path after stroke:', ctx.has_current_point())
