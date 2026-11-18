from pathlib import Path
from io import BytesIO
import base64,json,sys
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True)
asset=Image.new('RGBA',(40,40),(255,0,0,255));asset.putpixel((0,0),(0,0,0,0));b=BytesIO();asset.save(b,format='PNG');uri='data:image/png;base64,'+base64.b64encode(b.getvalue()).decode()
svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160"><rect x="10" y="10" width="80" height="80" fill="#ff0000"/><rect x="110" y="10" width="80" height="80" fill="#00ff00"/><image x="10" y="110" width="40" height="40" href="{uri}"/></svg>'
checks=[]
def run(name,**opts):
 data=cairosvg.svg2png(bytestring=svg.encode(),**opts);(OUT/(name+'.svg')).write_text(svg);(OUT/(name+'.png')).write_bytes(data);return Image.open(BytesIO(data)).convert('RGBA')
a=run('off');b=run('negate',negate_colors=True);d=run('invert',invert_images=True);e=run('both',negate_colors=True,invert_images=True)
for name,im,pts in [('off',a,[((20,20),(255,0,0,255)),((120,20),(0,255,0,255)),((10,110),(0,0,0,0))]),('negate',b,[((20,20),(0,255,255,255)),((120,20),(255,0,255,255))]),('invert',d,[((20,20),(255,0,0,255)),((20,120),(0,255,255,255)),((10,110),(0,0,0,0))]),('both',e,[((20,20),(0,255,255,255)),((20,120),(0,255,255,255))])]:
 for pt,want in pts:
  got=im.getpixel(pt);checks.append({'name':name+str(pt),'expected':want,'actual':got,'passed':got==want})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>颜色开关</title><style>body{font:20px system-ui}img{width:480px;image-rendering:pixelated;border:1px solid #888}</style><h1>矢量反色与图片反色</h1>'+''.join('<h2>'+n+'</h2><img src="'+n+'.png">' for n in ['off','negate','invert','both'])+'<pre>'+json.dumps(r,ensure_ascii=False,indent=2)+'</pre>');sys.exit(bool(r['failed']))
