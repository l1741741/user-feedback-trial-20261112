"""Empirical probe of current CairoSVG transform behavior."""
import io
import cairosvg
from PIL import Image

W = H = 200

def render(svg_body):
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">'
           f'{svg_body}</svg>').encode()
    return Image.open(io.BytesIO(cairosvg.svg2png(svg))).convert('RGBA')

def red_stats(img):
    px = img.load()
    pts = [(x, y) for y in range(H) for x in range(W)
           if px[x, y][3] and px[x, y][0] > 150 and px[x, y][1] < 80 and px[x, y][2] < 80]
    cx = sum(p[0] for p in pts)/len(pts); cy = sum(p[1] for p in pts)/len(pts)
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return f"center=({cx:.1f},{cy:.1f}) bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)}) n={len(pts)}"

# Case 1: rotate 90 about explicit viewport-center origin; rect itself centered there.
s1 = render('<rect x="70" y="90" width="60" height="20" fill="red" '
            'transform="rotate(90)" transform-origin="100 100"/>')
print("Case1 expect center(100,100) bbox(90,70)-(110,130):", red_stats(s1))

# Case 2: outer translate(50,50); inner origin "50 50" is local-user-space per spec
s2 = render('<g transform="translate(50,50)">'
            '<rect x="20" y="40" width="60" height="20" fill="red" '
            'transform="rotate(90)" transform-origin="50 50"/></g>')
print("Case2 expect center(100,100):", red_stats(s2))

# Case 3: composition order of nested transforms
s3 = render('<g transform="translate(100,100)"><g transform="rotate(90)">'
            '<rect x="40" y="0" width="10" height="10" fill="red"/></g></g>')
print("Case3 expect center ~(100,145):", red_stats(s3))

# Case 4: keyword "center" inside translated group: local (100,100) -> device (150,150)
s4 = render('<g transform="translate(50,50)">'
            '<rect x="70" y="90" width="60" height="20" fill="red" '
            'transform="rotate(90)" transform-origin="center"/></g>')
print("Case4 expect center(150,150):", red_stats(s4))

# Case 5: percentage origin under viewBox scale 2
svg5 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 100 100">'
        '<rect x="35" y="45" width="30" height="10" fill="red" '
        'transform="rotate(90)" transform-origin="50% 50%"/></svg>').encode()
s5 = Image.open(io.BytesIO(cairosvg.svg2png(svg5))).convert('RGBA')
px = s5.load()
pts = [(x, y) for y in range(H) for x in range(W)
       if px[x, y][3] and px[x, y][0] > 150 and px[x, y][1] < 80 and px[x, y][2] < 80]
cx = sum(p[0] for p in pts)/len(pts); cy = sum(p[1] for p in pts)/len(pts)
print(f"Case5 expect center(100,100): center=({cx:.1f},{cy:.1f})")
