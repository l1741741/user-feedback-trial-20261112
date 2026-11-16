import io, cairosvg
from PIL import Image
svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200" viewBox="0 0 100 100">'
       '<g transform="translate(25,25)">'
       '<rect x="10" y="20" width="30" height="10" fill="red" '
       'transform="rotate(90)" transform-origin="center"/></g></svg>')
img = Image.open(io.BytesIO(cairosvg.svg2png(svg.encode()))).convert('RGBA')
px = img.load()
pts = [(x,y) for y in range(200) for x in range(200) if px[x,y][3] and px[x,y][0]>150 and px[x,y][1]<80]
xs=[p[0] for p in pts]; ys=[p[1] for p in pts]
print("bbox", (min(xs),min(ys),max(xs),max(ys)), "center", (sum(xs)/len(xs), sum(ys)/len(ys)))
# hand calc in user units:
# origin local viewport center (50,50); rect center (25,25)
# rotate (25,25) about (50,50): delta(-25,-25) R90 -> (25,-25) -> (75,25)
# group translate -> (100,50) user -> device (200,100)
