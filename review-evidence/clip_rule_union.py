from pathlib import Path
from io import BytesIO
import sys,json
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[]
ring='M10 10H110V110H10Z M40 40H80V80H40Z'
scenes=[('fill-not-clip',f'<path d="{ring}" fill-rule="evenodd"/>',[(60,60,(255,0,0,255)),(20,20,(255,0,0,255))]),('duplicate-evenodd','<rect width="100" height="100" clip-rule="evenodd"/><rect width="100" height="100" clip-rule="evenodd"/>',[(50,50,(255,0,0,255)),(90,20,(255,0,0,255))]),('mixed-rules',f'<path d="{ring}" clip-rule="evenodd"/><rect x="140" y="10" width="40" height="100" clip-rule="nonzero"/>',[(60,60,(0,0,0,0)),(160,50,(255,0,0,255))])]
for name,content,probes in scenes:
 text=f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120"><defs><clipPath id="c">{content}</clipPath></defs><rect width="200" height="120" fill="red" clip-path="url(#c)"/></svg>';(OUT/(name+'.svg')).write_text(text);data=cairosvg.svg2png(bytestring=text.encode());(OUT/(name+'.png')).write_bytes(data);im=Image.open(BytesIO(data)).convert('RGBA')
 for x,y,wanted in probes:
  actual=im.getpixel((x,y));checks.append({'name':name+f'-{x}-{y}','expected':wanted,'actual':actual,'passed':actual==wanted})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>裁剪规则与子元素并集</title><style>body{font:20px system-ui;padding:20px}img{width:600px;border:1px solid #888;background:white;image-rendering:pixelated}</style><h1>分别计算clip-rule并合并子元素区域</h1>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>本次PNG</p><img src="'+n+'.png">' for n,_,_ in scenes)+'<pre>'+json.dumps(r,ensure_ascii=False,indent=2)+'</pre>');sys.exit(bool(r['failed']))
