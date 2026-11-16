import cairosvg
from PIL import Image
import io
def red_bbox(svg):
    img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
    px = img.load()
    pts = [(x,y) for y in range(200) for x in range(200) if px[x,y][3] and px[x,y][0]>150 and px[x,y][1]<80]
    if not pts: return "NO PIXELS"
    xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
    return (min(xs),min(ys),max(xs),max(ys))

# translate with non-numeric arg followed by another valid transform
print("bad translate then rotate:", red_bbox(
    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
    '<rect x="10" y="10" width="10" height="10" fill="red" transform="translate(garbage) rotate(45)"/></svg>'))

# scale with bad value
print("bad scale:", red_bbox(
    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
    '<rect x="10" y="10" width="10" height="10" fill="red" transform="scale(x) translate(50,50)"/></svg>'))

# rotate missing angle
print("rotate missing angle:", red_bbox(
    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
    '<rect x="10" y="10" width="10" height="10" fill="red" transform="rotate() translate(50,50)"/></svg>'))

# matrix with too few values
print("short matrix:", red_bbox(
    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
    '<rect x="10" y="10" width="10" height="10" fill="red" transform="matrix(1 2) translate(50,50)"/></svg>'))
