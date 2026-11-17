"""Inherited gradient versus explicit equivalent, with file cache and order checks."""
from pathlib import Path
from io import BytesIO
import html,json,sys
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[]
redblue='<stop stop-color="red"/><stop offset="1" stop-color="blue"/>';green='<stop stop-color="lime"/><stop offset="1" stop-color="aqua"/>'
base='<linearGradient id="b" x1="0" y1="0" x2="1" y2="0">'+redblue+'</linearGradient>'
defs=base+'<linearGradient id="mid" href="#b" x2="0" y2="1"/><linearGradient id="leaf" href="#mid"/><linearGradient id="own" href="#b">'+green+'</linearGradient><radialGradient id="r" cx=".4" cy=".5" r=".6">'+redblue+'</radialGradient><radialGradient id="rleaf" href="#r" fx=".2"/>'
flat='<linearGradient id="leaf" x1="0" y1="0" x2="0" y2="1">'+redblue+'</linearGradient><linearGradient id="own" x1="0" y1="0" x2="1" y2="0">'+green+'</linearGradient><radialGradient id="rleaf" cx=".4" cy=".5" r=".6" fx=".2">'+redblue+'</radialGradient>'
shapes=['<rect x="10" y="10" width="90" height="90" fill="url(#leaf)"/>','<rect x="120" y="10" width="90" height="90" fill="url(#own)" fill-opacity=".5"/>','<circle cx="275" cy="55" r="45" fill="url(#rleaf)"/>']
def doc(d,ss):return '<svg xmlns="http://www.w3.org/2000/svg" width="340" height="120"><defs>'+d+'</defs>'+''.join(ss)+'</svg>'
def render(name,text,file=False):
 path=OUT/(name+'.svg');path.write_text(text);data=cairosvg.svg2png(**({'url':str(path)} if file else {'bytestring':text.encode()}));(OUT/(name+'.png')).write_bytes(data);return Image.open(BytesIO(data)).convert('RGBA')
a=render('inherited',doc(defs,shapes));b=render('explicit-reference',doc(flat,shapes));checks.append({'name':'three-inherited-icons-match-explicit-definitions','passed':a.tobytes()==b.tobytes()})
rev=render('reverse-order',doc(defs,shapes[::-1]));checks.append({'name':'drawing-order-does-not-change-icons','passed':a.tobytes()==rev.tobytes()})
f=render('file-entry',doc(defs,shapes),True);checks.append({'name':'file-cache-entry-matches-bytestring','passed':a.tobytes()==f.tobytes()})
checks.append({'name':'multilevel-vertical-stops-reach-leaf','passed':a.getpixel((50,14))[0]>235 and a.getpixel((50,95))[2]>235})
checks.append({'name':'own-stops-and-opacity-retained','passed':a.getpixel((125,50))[1]>240 and 125<=a.getpixel((125,50))[3]<=130})
broken=defs+'<linearGradient id="c1" href="#c2"/><linearGradient id="c2" href="#c1"/><linearGradient id="missing" href="#absent"/>'
err=render('broken-plus-healthy',doc(broken,shapes+['<rect x="330" width="5" height="5" fill="url(#c1)"/>','<rect x="330" y="10" width="5" height="5" fill="url(#missing)"/>']));checks.append({'name':'broken-references-preserve-healthy-icons','passed':err.crop((0,0,325,120)).tobytes()==a.crop((0,0,325,120)).tobytes()})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks}
(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2))
(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>共享渐变继承</title><style>body{font:19px system-ui;margin:24px}img{width:850px;max-width:100%;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>多级继承、自有色标和径向覆盖</h1>'+''.join('<h2>'+n+'</h2><img src="'+n+'.png"><details><summary>SVG输入</summary><pre>'+html.escape((OUT/(n+'.svg')).read_text())+'</pre></details>' for n in ['inherited','explicit-reference','reverse-order','file-entry','broken-plus-healthy'])+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>')
sys.exit(bool(r['failed']))
