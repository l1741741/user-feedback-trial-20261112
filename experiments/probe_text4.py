import cairosvg
from PIL import Image
import io
def count(angle, origin_spec):
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
           f'<text x="50" y="100" font-size="30" fill="red" transform="rotate({angle})" '
           f'{origin_spec}>Hi</text></svg>')
    img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
    pts = [(x,y) for y in range(200) for x in range(200) if img.load()[x,y][3] and img.load()[x,y][0]>150]
    if not pts: return "NO PIXELS"
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return f"n={len(pts)} bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)})"

print("90 fill-box:    ", count(90, 'transform-origin="center" transform-box="fill-box"'))
print("180 fill-box:   ", count(180, 'transform-origin="center" transform-box="fill-box"'))
print("180 view-box:   ", count(180, 'transform-origin="center"'))
print("180 no origin:  ", count(180, ''))
print("180 explicit origin on bbox center (82.5,110.8):",
      count(180, 'transform-origin="82.5 110.8"'))
