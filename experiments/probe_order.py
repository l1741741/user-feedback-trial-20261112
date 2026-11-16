"""Check matrix composition order vs SVG spec on one element with list transforms."""
import io, math
import cairosvg
from PIL import Image

# Single element: transform="translate(100,100) rotate(90) scale(2)"
# Spec order: translate applied last to points => point p -> T * R * S * p
# rect (10,0) size 5x5 -> local center (12.5,2.5)
# S: (25,5); R90: (-5,25); T: (95,125)
svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
       '<rect x="10" y="0" width="5" height="5" fill="red" '
       'transform="translate(100,100) rotate(90) scale(2)"/></svg>')
img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
px = img.load()
pts = [(x, y) for y in range(200) for x in range(200)
       if px[x, y][3] and px[x, y][0] > 150 and px[x, y][1] < 80 and px[x, y][2] < 80]
cx = sum(p[0] for p in pts)/len(pts); cy = sum(p[1] for p in pts)/len(pts)
xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
print(f"list-transform: center=({cx:.1f},{cy:.1f}) bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)})")
print("spec-expect center (97.5,127.5), bbox (90,120)-(100,130) (10x10 device)")

# Two nested groups vs equivalent flat list — spec says they must match.
svg_nested = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
              '<g transform="translate(100,100)"><g transform="rotate(90)">'
              '<rect x="10" y="0" width="5" height="5" fill="red"/></g></g></svg>')
img2 = Image.open(io.BytesIO(cairosvg.svg2png(svg_nested.encode()))).convert('RGBA')
px = img2.load()
pts2 = [(x, y) for y in range(200) for x in range(200)
        if px[x, y][3] and px[x, y][0] > 150 and px[x, y][1] < 80 and px[x, y][2] < 80]
cx = sum(p[0] for p in pts2)/len(pts2); cy = sum(p[1] for p in pts2)/len(pts2)
xs = [p[0] for p in pts2]; ys = [p[1] for p in pts2]
print(f"nested-groups:  center=({cx:.1f},{cy:.1f}) bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)})")
print("spec-expect center (97.5,102.5), bbox (95,100)-(100,105)")
