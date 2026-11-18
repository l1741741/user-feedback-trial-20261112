from pathlib import Path
from io import BytesIO
import base64,json,sys
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True)
body='<rect x="10" y="10" width="50%" height="50%" fill="red"/>'
inner='<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="10 10 100 100">'+body+'</svg>';uri='data:image/svg+xml;base64,'+base64.b64encode(inner.encode()).decode()
text='<svg xmlns="http://www.w3.org/2000/svg" width="680" height="180"><rect width="680" height="180" fill="white"/><defs><symbol id="s" viewBox="10 10 100 100">'+body+'</symbol></defs><svg x="20" y="20" width="200" height="100" viewBox="10 10 100 100" preserveAspectRatio="xMidYMid meet">'+body+'</svg><use href="#s" x="240" y="20" width="200" height="100" preserveAspectRatio="xMidYMid meet"/><image href="'+uri+'" x="460" y="20" width="200" height="100" preserveAspectRatio="xMidYMid meet"/></svg>'
(OUT/'letterbox.svg').write_text(text);png=cairosvg.svg2png(bytestring=text.encode());(OUT/'letterbox.png').write_bytes(png);im=Image.open(BytesIO(png)).convert('RGBA');checks=[]
for name,left in [('nested',20),('symbol',240),('image',460)]:
 for label,point,wanted in [('inside',(left+60,30),(255,0,0,255)),('outside',(left+120,30),(255,255,255,255))]:
  actual=im.getpixel(point);checks.append({'name':name+'-'+label,'point':point,'actual':actual,'expected':wanted,'passed':actual==wanted})
reference=im.crop((460,20,660,120)).tobytes()
for name,left in [('nested',20),('symbol',240)]:checks.append({'name':name+'-equivalent-to-image','passed':im.crop((left,20,left+200,120)).tobytes()==reference})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks,'expected_semantics':'50% of viewBox width 100 is 50 user units; meet scale is one, with 50px horizontal offset'}
(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>非方形视口百分比核验</title><style>body{font:20px system-ui;margin:24px}img{width:1000px;max-width:100%;image-rendering:pixelated;border:1px solid #aaa}pre{white-space:pre-wrap}</style><h1>三种同等场景应得到相同的50px红色矩形</h1><p>从左到右：直接嵌套、symbol/use、image嵌入。非零viewBox原点，200×100视口使用meet。</p><h2>SVG输入</h2><img src="letterbox.svg"><h2>本次PNG</h2><img src="letterbox.png"><pre>'+json.dumps(r,ensure_ascii=False,indent=2)+'</pre>');sys.exit(bool(r['failed']))
