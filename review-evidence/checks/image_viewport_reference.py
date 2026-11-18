from pathlib import Path
from io import BytesIO
import sys,json,html,base64
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
# Raster and SVG describe the same two-color 40x20 source.
im=Image.new('RGB',(40,20),'red');im.paste('blue',(20,0,40,20));sink=BytesIO();im.save(sink,format='PNG');raster='data:image/png;base64,'+base64.b64encode(sink.getvalue()).decode();source='<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20" viewBox="10 20 40 20"><rect x="10" y="20" width="20" height="20" fill="red"/><rect x="30" y="20" width="20" height="20" fill="blue"/></svg>';vector='data:image/svg+xml;base64,'+base64.b64encode(source.encode()).decode()
for kind,uri in [('raster',raster),('svg',vector)]:
 for label,par in [('meet','xMidYMid meet'),('slice','xMaxYMid slice'),('none','none')]:
  n=kind+'-'+label;names.append(n);s='<svg xmlns="http://www.w3.org/2000/svg" width="150" height="120"><image x="10" y="10" width="80" height="80" preserveAspectRatio="'+par+'" href="'+uri+'"/><rect x="110" y="20" width="20" height="60" fill="lime"/></svg>';(OUT/(n+'.svg')).write_text(s);p=cairosvg.svg2png(bytestring=s.encode());(OUT/(n+'.png')).write_bytes(p);im=Image.open(BytesIO(p)).convert('RGBA')
  samples=[((20,20),(0,0,0,0)),((25,45),(255,0,0,255)),((75,45),(0,0,255,255))] if label=='meet' else [((20,20),(0,0,255,255)),((75,75),(0,0,255,255))] if label=='slice' else [((25,45),(255,0,0,255)),((75,45),(0,0,255,255))]
  samples+=[((100,45),(0,0,0,0)),((120,45),(0,255,0,255))]
  for pt,want in samples:
   actual=im.getpixel(pt);checks.append({'name':n+'-'+str(pt),'actual':actual,'expected':want,'passed':actual==want})
for kind,uri in [('raster',raster),('svg',vector)]:
 n=kind+'-zero';names.append(n);s='<svg xmlns="http://www.w3.org/2000/svg" width="150" height="120"><image x="10" y="10" width="0" height="80" href="'+uri+'"/><rect x="110" y="20" width="20" height="60" fill="lime"/></svg>';(OUT/(n+'.svg')).write_text(s);p=cairosvg.svg2png(bytestring=s.encode());(OUT/(n+'.png')).write_bytes(p);im=Image.open(BytesIO(p)).convert('RGBA');checks.append({'name':n+'-empty-image','passed':im.getpixel((20,20))==(0,0,0,0) and im.getpixel((120,45))==(0,255,0,255)})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>图像视口对照</title><style>body{font:20px system-ui;margin:24px}img{width:450px;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>栅格和非零viewBox SVG：meet、slice、none</h1><p>meet上下留白；右对齐slice仅保留蓝半；none红蓝各半，绿色兄弟独立。</p>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
