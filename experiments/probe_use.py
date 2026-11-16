"""Check state mutation across repeated <use> of one node."""
import io
import cairosvg
from PIL import Image

# Three uses; use() deletes x/y/viewBox/mask from the TREE (cached/shared?) each time
svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="100">'
       '<defs><rect id="r" x="0" y="0" width="20" height="20" fill="red"/></defs>'
       '<use href="#r" x="20" y="40"/>'
       '<use href="#r" x="120" y="40"/>'
       '<use href="#r" x="220" y="40"/></svg>')
img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
px = img.load()
pts = [(x, y) for y in range(100) for x in range(300)
       if px[x, y][3] and px[x, y][0] > 150 and px[x, y][1] < 80 and px[x, y][2] < 80]
clusters = {}
for x, y in pts:
    clusters.setdefault(x // 100, []).append((x, y))
for k, v in sorted(clusters.items()):
    cx = sum(p[0] for p in v)/len(v); cy = sum(p[1] for p in v)/len(v)
    print(f"use cluster x//100={k}: center=({cx:.1f},{cy:.1f}) n={len(v)} "
          f"(expect ~30,150,250 at y=50)")

# Same Tree rendered twice in one process: bounding_box cache on node dict
svg2 = ('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<g id="g"><rect x="10" y="10" width="20" height="20" fill="red"/></g></svg>')
out1 = cairosvg.svg2png(svg2.encode())
out2 = cairosvg.svg2png(svg2.encode())
print("two independent renders identical:", out1 == out2)

# Two uses where the referenced g has its own transform-origin
svg3 = ('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200" viewBox="0 0 300 200">'
        '<defs><g id="g"><rect x="0" y="0" width="40" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="20 10"/></g></defs>'
        '<use href="#g" x="50" y="50"/><use href="#g" x="150" y="100"/></svg>')
img3 = Image.open(io.BytesIO(cairosvg.svg2png(svg3.encode()))).convert('RGBA')
px = img3.load()
pts = [(x, y) for y in range(200) for x in range(300)
       if px[x, y][3] and px[x, y][0] > 150 and px[x, y][1] < 80 and px[x, y][2] < 80]
clusters = {}
for x, y in pts:
    clusters.setdefault((x // 100, y // 100), []).append((x, y))
for k, v in sorted(clusters.items()):
    cx = sum(p[0] for p in v)/len(v); cy = sum(p[1] for p in v)/len(v)
    xs = [p[0] for p in v]; ys = [p[1] for p in v]
    print(f"use+origin cluster {k}: center=({cx:.1f},{cy:.1f}) "
          f"bbox=({min(xs)},{min(ys)})-({max(xs)},{max(ys)}) n={len(v)}")
# expect cluster1 center (70,70), cluster2 center (170,120); each 20x40 bbox
