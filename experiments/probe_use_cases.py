"""use with viewBox, mask, nested svg — check behavior after removing deletions."""
import io
import cairosvg
from PIL import Image

def red_count(svg, size=200):
    img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
    px = img.load()
    return sum(1 for y in range(size) for x in range(size)
               if px[x, y][3] and px[x, y][0] > 150 and px[x, y][1] < 80 and px[x, y][2] < 80)

# use with viewBox on referenced svg
svg1 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<defs><symbol id="s" viewBox="0 0 100 100">'
        '<rect x="0" y="0" width="100" height="100" fill="red"/></symbol></defs>'
        '<use href="#s" x="20" y="20" width="80" height="80"/></svg>')
print("symbol use red count (expect ~81x81=6561):", red_count(svg1))

# use with mask
svg2 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<defs><mask id="m"><rect x="0" y="0" width="50" height="50" fill="white"/></mask>'
        '<rect id="r" width="100" height="100" fill="red"/></defs>'
        '<use href="#r" x="50" y="50" mask="url(#m)"/></svg>')
print("masked use red count (expect 51x51=2601):", red_count(svg2))

# use twice referencing symbol, both must render identically sized
svg3 = ('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="100">'
        '<defs><symbol id="s" viewBox="0 0 50 50"><rect width="50" height="50" fill="red"/></symbol></defs>'
        '<use href="#s" x="10" y="25" width="50" height="50"/>'
        '<use href="#s" x="120" y="25" width="50" height="50"/></svg>')
img = Image.open(io.BytesIO(cairosvg.svg2png(svg3.encode()))).convert('RGBA')
px = img.load()
pts = [(x,y) for y in range(100) for x in range(300) if px[x,y][3] and px[x,y][0]>150 and px[x,y][1]<80]
c1 = [p for p in pts if p[0] < 100]; c2 = [p for p in pts if p[0] >= 100]
print(f"two symbol uses: n1={len(c1)} n2={len(c2)} (expect equal, ~2601 each)")

# same-tree double render of symbol uses
out1 = cairosvg.svg2png(svg3.encode())
out2 = cairosvg.svg2png(svg3.encode())
print("idempotent:", out1 == out2)
