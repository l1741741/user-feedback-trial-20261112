from pathlib import Path
from io import BytesIO
import sys,json
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[]
for name,attrs in [('hidden','visibility="hidden"'),('collapse','visibility="collapse"'),('display-none','display="none"')]:
 text=f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120"><defs><clipPath id="c"><rect width="100" height="100" {attrs}/></clipPath></defs><rect width="100" height="100" fill="red" clip-path="url(#c)"/><rect x="120" y="20" width="60" height="60" fill="blue"/></svg>';(OUT/(name+'.svg')).write_text(text);data=cairosvg.svg2png(bytestring=text.encode());(OUT/(name+'.png')).write_bytes(data);im=Image.open(BytesIO(data)).convert('RGBA')
 for label,point,wanted in [('clip',(50,50),(0,0,0,0)),('sibling',(150,50),(0,0,255,255))]:
  actual=im.getpixel(point);checks.append({'name':name+'-'+label,'point':point,'expected':wanted,'actual':actual,'passed':actual==wanted})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>裁剪可见性复核</title><style>body{font:20px system-ui;padding:20px}img{width:600px;border:1px solid #888;background:white;image-rendering:pixelated}</style><h1>隐藏裁剪子元素不应留下红色，蓝色兄弟应完整</h1>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>本次PNG</p><img src="'+n+'.png">' for n in ['hidden','collapse','display-none'])+'<pre>'+json.dumps(r,ensure_ascii=False,indent=2)+'</pre>');sys.exit(bool(r['failed']))
