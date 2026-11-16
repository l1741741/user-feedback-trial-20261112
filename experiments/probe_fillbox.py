import io
import cairosvg
from PIL import Image

def stats(body, size=200, svg_attrs=''):
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" {svg_attrs}>'
           f'{body}</svg>')
    img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
    px = img.load()
    pts = [(x, y) for y in range(size) for x in range(size)
           if px[x, y][3] and px[x, y][0] > 150 and px[x, y][1] < 80 and px[x, y][2] < 80]
    if not pts: return "NO RED PIXELS"
    cx = sum(p[0] for p in pts)/len(pts); cy = sum(p[1] for p in pts)/len(pts)
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return f"center=({cx:.1f},{cy:.1f}) bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)}) n={len(pts)}"

# fill-box: off-center rect (60,80) 40x20 rotated 90 about its own center (80,90)
print("1 fill-box center, expect center(80,90) bbox(70,70)-(90,109):",
      stats('<rect x="60" y="80" width="40" height="20" fill="red" '
            'transform="rotate(90)" transform-origin="center" transform-box="fill-box"/>'))

# fill-box with explicit percentage origin: 0% 0% = rect top-left (60,80) stays fixed.
# Rotation of rect about (60,80): rect center (80,90) -> (70,100)
print("2 fill-box 0% 0%, expect center(70,100):",
      stats('<rect x="60" y="80" width="40" height="20" fill="red" '
            'transform="rotate(90)" transform-origin="0 0" transform-box="fill-box"/>'))

# fill-box 50% keyword top: origin x=center(80), y=top(80)
# rect center (80,90) rotated about (80,80) -> (70,80)
print("3 fill-box center top, expect center(70,80):",
      stats('<rect x="60" y="80" width="40" height="20" fill="red" '
            'transform="rotate(90)" transform-origin="center top" transform-box="fill-box"/>'))

# degenerate: zero-width rect + fill-box must not crash and should render something
print("4 degenerate fill-box:",
      stats('<rect x="60" y="80" width="0" height="20" fill="red" '
            'transform="rotate(90)" transform-origin="center" transform-box="fill-box"/>'
            '<rect x="100" y="100" width="10" height="10" fill="red"/>'))

# illegal values: malformed origin ignored -> rotation about (0,0); second rect unaffected
print("5 'garbage' isolation:",
      stats('<rect x="10" y="10" width="20" height="20" fill="red" '
            'transform="rotate(90)" transform-origin="garbage"/>'
            '<rect x="50" y="50" width="20" height="20" fill="red"/>'))
print("6 empty string isolation:",
      stats('<rect x="10" y="10" width="20" height="20" fill="red" '
            'transform="rotate(90)" transform-origin=""/>'
            '<rect x="50" y="50" width="20" height="20" fill="red"/>'))
print("7 3 tokens illegal:",
      stats('<rect x="10" y="10" width="20" height="20" fill="red" '
            'transform="rotate(90)" transform-origin="1 2 3"/>'
            '<rect x="50" y="50" width="20" height="20" fill="red"/>'))
print("8 unknown transform-box falls back to view-box:",
      stats('<rect x="70" y="90" width="60" height="20" fill="red" '
            'transform="rotate(90)" transform-origin="center" transform-box="border-box"/>'))

# unit lengths: 1in = 96px
print("9 inch origin (96,96) with view-box, rect centered there, expect center(96,96):",
      stats('<rect x="66" y="86" width="60" height="20" fill="red" '
            'transform="rotate(90)" transform-origin="1in 1in"/>'))
