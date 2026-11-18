from pathlib import Path
from io import BytesIO
import json,sys,cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);s='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160"><rect x="10" y="10" width="60" height="60" fill="#0f08"/><rect x="90" y="10" width="60" height="60" fill="rgb(0 255 0 / 50%)"/><linearGradient id="g"><stop stop-color="#ff000080"/><stop offset="1" stop-color="#0000ff80"/></linearGradient><rect x="170" y="10" width="60" height="60" fill="url(#g)"/></svg>';checks=[]
for n,opts in [('off',{}),('negate',{'negate_colors':True})]:
 d=cairosvg.svg2png(bytestring=s.encode(),**opts);(OUT/(n+'.svg')).write_text(s);(OUT/(n+'.png')).write_bytes(d);im=Image.open(BytesIO(d)).convert('RGBA');checks += [{'name':n+'-alpha','actual':im.getpixel((20,20)),'passed':im.getpixel((20,20))[3]==136},{'name':n+'-modern-alpha','actual':im.getpixel((100,20)),'passed':im.getpixel((100,20))[3] in (127,128)}]
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>CSS颜色</title><style>body{font:20px system-ui}img{width:600px;image-rendering:pixelated}</style><h1>alpha颜色输入与实际PNG</h1><img src="off.png"><img src="negate.png"><pre>'+json.dumps(r,ensure_ascii=False,indent=2)+'</pre>');sys.exit(bool(r['failed']))
