import cairosvg
from PIL import Image
import io
svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
       '<text x="50" y="100" font-size="30" fill="red" transform="rotate(180)" '
       'transform-origin="center" transform-box="fill-box">Hi</text></svg>')
img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
px = img.load()
pts = [(x,y) for y in range(200) for x in range(200) if px[x,y][3]]
red = [(x,y) for x,y in pts if px[x,y][0]>150 and px[x,y][1]<80]
print("all opaque:", len(pts), "red:", len(red))
if pts:
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    print("bbox:", (min(xs),min(ys),max(xs),max(ys)))
# compare: same text rotated 180 about (0,0)
svg0 = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<text x="50" y="100" font-size="30" fill="red" transform="rotate(180)">Hi</text></svg>')
img0 = Image.open(io.BytesIO(cairosvg.svg2png(svg0.encode()))).convert('RGBA')
px0 = img0.load()
pts0 = [(x,y) for y in range(200) for x in range(200) if px0[x,y][3] and px0[x,y][0]>150]
print("no-origin red count:", len(pts0))
