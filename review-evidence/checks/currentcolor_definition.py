"""Independent definition-vs-instance colour probes, with real PNG outputs."""
from pathlib import Path
from io import BytesIO
import json,sys
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True)
SVG='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120"><defs><linearGradient id="g" color="red"><stop stop-color="currentColor"/><stop offset="1" stop-color="currentColor"/></linearGradient></defs><rect width="100" height="100" fill="url(#g)"/><rect x="120" width="100" height="100" fill="url(#g)" color="blue"/></svg>'
(OUT/'definition.svg').write_text(SVG);data=cairosvg.svg2png(bytestring=SVG.encode());(OUT/'definition.png').write_bytes(data);im=Image.open(BytesIO(data)).convert('RGBA')
checks=[{'name':'gradient-definition-inherits-red','point':[50,50],'expected':[255,0,0,255],'actual':list(im.getpixel((50,50)))},{'name':'paint-target-colour-does-not-replace-gradient-definition','point':[170,50],'expected':[255,0,0,255],'actual':list(im.getpixel((170,50)))}]
SVG2='<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"><defs><linearGradient id="g" color="red"><stop color="lime" stop-color="currentColor"/><stop offset="1" color="lime" stop-color="currentColor"/></linearGradient></defs><rect width="100" height="100" fill="url(#g)" color="blue"/></svg>'
(OUT/'stop-override.svg').write_text(SVG2);data=cairosvg.svg2png(bytestring=SVG2.encode());(OUT/'stop-override.png').write_bytes(data);im=Image.open(BytesIO(data)).convert('RGBA');checks.append({'name':'stop-local-colour-overrides-inherited-colour','point':[50,50],'expected':[0,255,0,255],'actual':list(im.getpixel((50,50)))})
for c in checks:c['passed']=c['actual']==c['expected']
result={'renderer':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'definition-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
# Images are regenerated above on each invocation; SVG itself is the expected visual reference.
(OUT/'demo.html').write_text('<!doctype html><meta charset="utf-8"><title>色标颜色继承核验</title><style>body{font:20px system-ui;margin:30px;background:#eee}section{display:inline-block;vertical-align:top;background:white;padding:20px;margin:10px}img{width:480px;image-rendering:pixelated;border:1px solid #aaa}pre{white-space:pre-wrap}</style><h1>色标颜色继承：输入与本次PNG输出</h1><p>定义色为红色时，左右图形都应使用红色渐变；下方局部覆盖应为绿色。</p><section><h2>SVG输入</h2><img src="definition.svg"><h2>PNG实际输出</h2><img src="definition.png"></section><section><h2>色标局部覆盖SVG</h2><img src="stop-override.svg"><h2>PNG实际输出</h2><img src="stop-override.png"></section><pre>'+json.dumps(checks,ensure_ascii=False,indent=2)+'</pre>')
print(json.dumps(result,ensure_ascii=False,indent=2));sys.exit(bool(result['failed']))
