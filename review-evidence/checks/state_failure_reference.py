from pathlib import Path
from io import BytesIO
import sys,json,html
import cairosvg
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface,cairo
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
head='<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120">';baseline=head+'<rect x="10" y="10" width="60" height="60" fill="red"/><rect x="150" y="20" width="60" height="60" fill="blue"/></svg>';special=head+'<defs><clipPath id="c"><rect width="20" height="20"/></clipPath></defs><g transform="translate(100 80) rotate(15)" opacity=".4" clip-path="url(#c)"><rect width="50" height="50" fill="green"/></g>'+baseline[len(head):];bad=head+'<g opacity=".5" transform="translate(30 20)"><image width="50" height="50" href="data:image/png;base64,bm90LWFuLWltYWdlLWJ5dGVz"/></g></svg>'
def render(n,text):
 names.append(n);(OUT/(n+'.svg')).write_text(text);p=cairosvg.svg2png(bytestring=text.encode());(OUT/(n+'.png')).write_bytes(p);return Image.open(BytesIO(p)).convert('RGBA')
a=render('baseline',baseline);b=render('special-before',special);checks.append({'name':'blue-sibling-region-unaffected','passed':a.crop((140,10,220,90)).tobytes()==b.crop((140,10,220,90)).tobytes()})
(OUT/'bad-input.svg').write_text(bad);caught=None;instances=[]
class RetainedSurface(PNGSurface):
 def __init__(self,*args,**kwargs):
  instances.append(self);super().__init__(*args,**kwargs)
try:RetainedSurface(Tree(bytestring=bad.encode()),BytesIO(),96)
except Exception as error:caught=type(error).__name__
checks.append({'name':'real-invalid-image-error-propagates','error':caught,'passed':caught is not None})
s=instances[0];checks.append({'name':'failed-surface-context-released','passed':'context' not in s.__dict__});finished=False
try:cairo.Context(s.cairo).paint()
except cairo.Error:finished=True
checks.append({'name':'failed-native-surface-finished','passed':finished})
c=render('after-failure',baseline);checks.append({'name':'fresh-export-after-mid-render-failure-matches-baseline','passed':a.tobytes()==c.tobytes()})
# Retain a successfully constructed surface and fail a subsequent draw on that same context.
class Kept(PNGSurface):pass
sink=BytesIO();kept=Kept(Tree(bytestring=(head+'</svg>').encode()),sink,96);before=(kept.context.get_matrix().as_tuple(),kept.context.get_operator(),kept.context.clip_extents(),kept.context_width,kept.context_height,kept.stroke_and_fill);err=None
try:kept.draw(Tree(bytestring=bad.encode()))
except Exception as error:err=type(error).__name__
after=(kept.context.get_matrix().as_tuple(),kept.context.get_operator(),kept.context.clip_extents(),kept.context_width,kept.context_height,kept.stroke_and_fill);checks.append({'name':'same-surface-frame-restored-after-error','error':err,'passed':err is not None and before==after});kept.draw(Tree(bytestring=baseline.encode()));kept.finish();p=sink.getvalue();(OUT/'same-surface-after-error.png').write_bytes(p);im=Image.open(BytesIO(p)).convert('RGBA');checks.append({'name':'same-surface-after-error-pixels-match-baseline','passed':im.tobytes()==a.tobytes()})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>中途失败后的状态与画布</title><style>body{font:20px system-ui;margin:24px}img{width:600px;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>正常、特殊前置与真实图像解析失败后渲染</h1>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<h2>同画布中途失败后绘制</h2><img src="same-surface-after-error.png"><pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
