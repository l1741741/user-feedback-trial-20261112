from pathlib import Path
from io import BytesIO
import sys,json,html
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
def render(n,body):
 names.append(n);s='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120">'+body+'</svg>';(OUT/(n+'.svg')).write_text(s);png=cairosvg.svg2png(bytestring=s.encode());(OUT/(n+'.png')).write_bytes(png);return Image.open(BytesIO(png)).convert('RGBA')
for mode in ['basic','offset','viewbox','quarter-turn','half-opacity']:
 transform=' patternTransform="translate(5 0)"' if mode=='offset' else ' patternTransform="rotate(90)"' if mode=='quarter-turn' else ''
 viewbox=' viewBox="0 0 10 10"' if mode=='viewbox' else ''
 tile='<rect width="5" height="10" fill="red"/>' if mode=='viewbox' else '<rect width="10" height="20" fill="red"/>'
 opacity=' opacity="0.5"' if mode=='half-opacity' else ''
 actual=render(mode,'<defs><pattern id="p" patternUnits="userSpaceOnUse" width="20" height="20"'+viewbox+transform+'>'+tile+'</pattern></defs><rect width="200" height="100" fill="url(#p)"'+opacity+'/><rect x="210" y="20" width="20" height="60" fill="blue"/>')
 tiles=[]
 if mode=='quarter-turn':
  for y in range(0,100,20):tiles.append(f'<rect y="{y}" width="200" height="10" fill="red"/>')
 else:
  for x in range(5 if mode=='offset' else 0,200,20):tiles.append(f'<rect x="{x}" width="{min(10,200-x)}" height="100" fill="red"/>')
 ref=render(mode+'-reference','<g'+opacity+'>'+''.join(tiles)+'</g><rect x="210" y="20" width="20" height="60" fill="blue"/>');checks.append({'name':mode+'-explicit-tiles','passed':actual.tobytes()==ref.tobytes(),'different_pixels':sum(a!=b for a,b in zip(actual.getdata(),ref.getdata()))});checks.append({'name':mode+'-blue-sibling','passed':actual.getpixel((220,50))==(0,0,255,255)})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>花纹与显式网格</title><style>body{font:20px system-ui;margin:24px}img{width:600px;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>花纹与逐块绘制的独立参考</h1>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
