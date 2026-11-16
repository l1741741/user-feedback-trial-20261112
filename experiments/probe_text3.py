import cairosvg
from PIL import Image
import io
for label, svg in [
  ("plain", '<text x="50" y="100" font-size="30" fill="red">Hi</text>'),
  ("translate", '<text x="50" y="100" font-size="30" fill="red" transform="translate(0,0)">Hi</text>'),
]:
    full = f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">{svg}</svg>'
    img = Image.open(io.BytesIO(cairosvg.svg2png(full.encode()))).convert('RGBA')
    n = sum(1 for y in range(200) for x in range(200) if img.load()[x,y][3])
    print(label, "opaque pixels:", n)
