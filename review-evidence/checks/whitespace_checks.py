"""Independent XML whitespace equivalence checks, real same-font SVG->PNG."""
from pathlib import Path
from io import BytesIO
import json,sys,traceback,html
import cairosvg
from PIL import Image,ImageChops
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];images=[]
def doc(body):return '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180"><g fill="red" font-family="DejaVu Sans Mono" font-size="24">'+body+'</g></svg>'
def render(name,body):
 text=doc(body);(OUT/(name+'.svg')).write_text(text);data=cairosvg.svg2png(bytestring=text.encode());(OUT/(name+'.png')).write_bytes(data);images.append(name);return Image.open(BytesIO(data)).convert('RGBA')
def equivalent(name,a,b):
 try:
  ia=render(name+'-case',a);ib=render(name+'-equivalent',b);delta=ImageChops.difference(ia,ib)
  extrema=[v[1] for v in delta.getextrema()];assert max(extrema)<=2,{'channel_max_difference':extrema,'case_box':ia.getchannel('A').getbbox(),'reference_box':ib.getchannel('A').getbbox()}
  checks.append({'name':name,'passed':True})
 except Exception as e:checks.append({'name':name,'passed':False,'error':str(e),'traceback':traceback.format_exc()})
equivalent('default-newline-tab-collapse','<text x="30" y="70"> \n A\n B\t   C \n </text>','<text x="30" y="70">A B C</text>')
equivalent('preserve-newline-tab-to-spaces','<text x="30" y="70" xml:space="preserve"> A\t \nB </text>','<text x="30" y="70" xml:space="preserve"> A   B </text>')
equivalent('span-boundary-spaces','<text x="30" y="70"> A <tspan>B</tspan> C </text>','<text x="30" y="70">A B C</text>')
equivalent('empty-span-consolidates-space','<text x="30" y="70">A <tspan/> B</text>','<text x="30" y="70">A B</text>')
equivalent('same-text-repeat','<text x="30" y="70" xml:space="preserve">  A  B </text>','<text x="30" y="70" xml:space="preserve">  A  B </text>')
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks,'oracle_reference':'SVG 1.1 text 10.15 whitespace; same local monospaced font, rendered equivalences'}
(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>文本空白等价核验</title><style>body{font:18px system-ui;margin:25px;background:#eee}section{display:inline-block;background:white;padding:12px;margin:8px}img{width:480px;border:1px solid #aaa;image-rendering:pixelated}pre{white-space:pre-wrap}</style><h1>SVG文本空白处理</h1><p>每组展示原输入与等价文本的当次PNG，同一本地字体。</p>'+''.join('<section><h2>'+n+'</h2><img src="'+n+'.png"><pre>'+html.escape((OUT/(n+'.svg')).read_text())+'</pre></section>' for n in images)+'<pre>'+json.dumps(r,ensure_ascii=False,indent=2)+'</pre>');sys.exit(bool(r['failed']))
