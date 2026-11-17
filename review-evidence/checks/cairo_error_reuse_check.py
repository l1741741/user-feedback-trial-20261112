"""Exercise an actual native Cairo error, then reuse the same drawing entry."""
from pathlib import Path
from io import BytesIO
import html,json,sys,traceback
import cairosvg
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True)
def doc(body):
 return '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160">'+body+'</svg>'
blue=doc('<rect x="150" y="20" width="60" height="60" fill="blue"/>')
bad=doc('<g opacity=".5" transform="translate(20 20)"><path d="M10 10H100" stroke="red" stroke-dasharray="-1 3"/></g>')
(OUT/'bad-layer.svg').write_text(bad);(OUT/'reference.svg').write_text(blue)
checks=[];errors=[]
reference=cairosvg.svg2png(bytestring=blue.encode());(OUT/'reference.png').write_bytes(reference)
checks.append({'name':'fresh-public-conversion-has-blue-rect','passed':Image.open(BytesIO(reference)).convert('RGBA').getpixel((170,40))==(0,0,255,255)})
output=BytesIO();surface=PNGSurface(Tree(bytestring=doc('').encode()),output,96)
original=None
try:
 surface.draw(Tree(bytestring=bad.encode()))
except Exception as error:
 original={'type':type(error).__name__,'message':str(error),'traceback':traceback.format_exc()};errors.append({'phase':'original-layer',**original})
checks.append({'name':'actual-native-error-propagates','passed':original is not None and 'INVALID_DASH' in original['message'],'actual_error':original})
try:
 surface.draw(Tree(bytestring=blue.encode()));surface.finish()
 data=output.getvalue();(OUT/'after-failure.png').write_bytes(data)
 same=Image.open(BytesIO(data)).convert('RGBA').tobytes()==Image.open(BytesIO(reference)).convert('RGBA').tobytes()
 checks.append({'name':'same-entry-recovers-and-renders-blue','passed':same})
except Exception as error:
 detail={'type':type(error).__name__,'message':str(error),'traceback':traceback.format_exc()};errors.append({'phase':'next-draw',**detail})
 checks.append({'name':'same-entry-recovers-and-renders-blue','passed':False,'actual_error':detail,'output_png_created':False})
second=cairosvg.svg2png(bytestring=blue.encode());(OUT/'fresh-after-error.png').write_bytes(second)
checks.append({'name':'fresh-public-conversion-after-error-matches-reference','passed':Image.open(BytesIO(second)).convert('RGBA').tobytes()==Image.open(BytesIO(reference)).convert('RGBA').tobytes()})
try:
 surface.finish()
except Exception as error:
 errors.append({'phase':'cleanup','type':type(error).__name__,'message':str(error)})
(OUT/'actual-errors.json').write_text(json.dumps(errors,ensure_ascii=False,indent=2)+'\n')
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks,'scope':'same PNGSurface.draw entry reused after an actual Cairo native error; fresh public conversions are controls'}
(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2))
(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>Cairo错误后复用入口</title><style>body{font:19px system-ui;margin:24px}img{width:480px;max-width:100%;border:1px solid #999;background:#eee}pre{white-space:pre-wrap}</style><h1>原生Cairo错误后，同一绘制入口仍不能继续</h1><h2>触发错误的实际SVG</h2><pre>'+html.escape(bad)+'</pre><h2>后续应绘制的蓝色矩形SVG</h2><pre>'+html.escape(blue)+'</pre><h2>独立入口生成的本次参考PNG</h2><img src="reference.png"><h2>出错后新建入口的本次PNG</h2><img src="fresh-after-error.png"><h2>同一入口的真实结果</h2><p>再次绘制抛出INVALID_DASH，未生成PNG。下面为实际异常及检查结果。</p><pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>')
sys.exit(bool(r['failed']))
