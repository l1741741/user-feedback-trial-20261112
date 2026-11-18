from pathlib import Path
from io import BytesIO
import sys,json
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True)
checks=[]
for mode,width,height in [('fraction','.5','1'),('percentage','50%','100%')]:
 svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120"><defs><mask id="m" maskContentUnits="objectBoundingBox" maskUnits="objectBoundingBox" x="0" y="0" width="1" height="1"><rect width="{width}" height="{height}" fill="white"/></mask></defs><rect x="20" y="20" width="80" height="60" fill="red" mask="url(#m)"/><rect x="130" y="20" width="50" height="60" fill="blue"/></svg>'
 (OUT/(mode+'.svg')).write_text(svg);png=cairosvg.svg2png(bytestring=svg.encode());(OUT/(mode+'.png')).write_bytes(png);im=Image.open(BytesIO(png)).convert('RGBA')
 for label,point,expected in [('left',(30,50),(255,0,0,255)),('right',(80,50),(0,0,0,0)),('sibling',(150,50),(0,0,255,255))]:
  actual=im.getpixel(point);checks.append({'name':mode+'-'+label,'point':point,'expected':expected,'actual':actual,'passed':actual==expected})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2))
(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>对象边界遮罩</title><style>body{font:20px system-ui;padding:20px}img{width:600px;border:1px solid #888;background:white;image-rendering:pixelated}</style><h1>等价的半宽遮罩</h1><p>两种写法均应只保留红色对象左半，蓝色兄弟应完整。</p>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG输出</p><img src="'+n+'.png">' for n in ['fraction','percentage'])+'<pre>'+json.dumps(r,ensure_ascii=False,indent=2)+'</pre>');sys.exit(bool(r['failed']))
