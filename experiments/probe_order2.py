"""Simpler order probes to locate the anomaly."""
import io
import cairosvg
from PIL import Image

def stats(body, size=200):
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}">'
           f'{body}</svg>')
    img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
    px = img.load()
    pts = [(x, y) for y in range(size) for x in range(size)
           if px[x, y][3] and px[x, y][0] > 150 and px[x, y][1] < 80 and px[x, y][2] < 80]
    if not pts: return "NO PIXELS"
    cx = sum(p[0] for p in pts)/len(pts); cy = sum(p[1] for p in pts)/len(pts)
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return f"center=({cx:.1f},{cy:.1f}) bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)})"

print("1 plain:            ", stats('<rect x="10" y="0" width="5" height="5" fill="red"/>'))
print("2 rotate90 alone:   ", stats('<rect x="10" y="0" width="5" height="5" fill="red" transform="rotate(90)"/>'))
print("3 flat T then R:    ", stats('<rect x="10" y="0" width="5" height="5" fill="red" transform="translate(100,100) rotate(90)"/>'), "expect bbox(95,110)-(100,114)")
print("4 nested g+rect:    ", stats('<g transform="translate(100,100)"><rect x="10" y="0" width="5" height="5" fill="red" transform="rotate(90)"/></g>'))
print("5 nested g/g:       ", stats('<g transform="translate(100,100)"><g transform="rotate(90)"><rect x="10" y="0" width="5" height="5" fill="red"/></g></g>'))
print("6 g translate only: ", stats('<g transform="translate(100,100)"><rect x="10" y="0" width="5" height="5" fill="red"/></g>'), "expect bbox(110,100)-(114,104)")
