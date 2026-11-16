import cairosvg, io
from PIL import Image

def render_pixels(svg):
    buf = io.BytesIO()
    cairosvg.svg2png(svg.encode(), write_to=buf)
    img = Image.open(io.BytesIO(buf.getvalue())).convert('RGBA')
    return [(x,y) for y in range(200) for x in range(200)
            if img.load()[x,y][3] and img.load()[x,y][0] > 150]

base = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<text x="50" y="100" font-size="30" fill="red" transform="rotate(180)" {spec}>Hi</text></svg>')

p_fill = render_pixels(base.format(spec='transform-origin="center" transform-box="fill-box"'))
p_explicit = render_pixels(base.format(spec='transform-origin="82.49755859375 110.75927734375"'))
print("fill-box n:", len(p_fill), "explicit n:", len(p_explicit))

# Try a shape (rect) fill-box to confirm the path works outside text
rect_svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
            '<rect x="40" y="60" width="80" height="40" fill="red" transform="rotate(180)" '
            'transform-origin="center" transform-box="fill-box"/></svg>')
p_rect = render_pixels(rect_svg)
xs=[p[0] for p in p_rect]; ys=[p[1] for p in p_rect]
print("rect fill-box 180 bbox:", (min(xs),min(ys),max(xs),max(ys)))
