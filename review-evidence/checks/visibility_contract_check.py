"""Check visibility on resource boundaries and display on non-rendered stops."""
from pathlib import Path
from io import BytesIO
import base64,html,json,sys
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
def svg(body):return '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'+body+'</svg>'
def render(n,text):
 names.append(n);(OUT/(n+'.svg')).write_text(text);data=cairosvg.svg2png(bytestring=text.encode());(OUT/(n+'.png')).write_bytes(data);return Image.open(BytesIO(data)).convert('RGBA')
def check(n,passed,**detail):checks.append({'name':n,'passed':bool(passed),**detail})
defs='<defs><linearGradient id="g"><stop offset="0" stop-color="red" display="none"/><stop offset="1" stop-color="blue"/></linearGradient></defs><rect width="200" height="100" fill="url(#g)"/>'
a=render('gradient-stop-display-none',svg(defs));b=render('gradient-stop-reference',svg(defs.replace(' display="none"','')))
check('display-does-not-remove-gradient-stop',a.tobytes()==b.tobytes());px=a.getpixel((5,50));check('left-stop-retains-red-contribution',px[0]>240 and px[2]<15,actual=px,reference=b.getpixel((5,50)))
inner='<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="40" height="40" fill="red" visibility="visible"/></svg>';uri='data:image/svg+xml;base64,'+base64.b64encode(inner.encode()).decode();body='<image x="5" y="5" width="40" height="40" visibility="hidden" href="'+uri+'"/><rect x="100" y="20" width="50" height="50" fill="blue"/>'
im=render('hidden-svg-image',svg(body));px=im.getpixel((25,25));check('hidden-image-cannot-be-overridden-by-resource-child',px==(0,0,0,0),actual=px,expected=[0,0,0,0]);check('hidden-image-keeps-blue-sibling',im.getpixel((120,40))==(0,0,255,255))
visible=render('visible-svg-image',svg(body.replace('visibility="hidden"','visibility="visible"')));check('visible-image-renders-child',visible.getpixel((25,25))==(255,0,0,255))
group=render('visibility-vs-display',svg('<g visibility="hidden"><rect width="40" height="40" visibility="visible" fill="lime"/></g><g display="none"><rect x="50" width="40" height="40" visibility="visible" fill="red"/></g><rect x="120" width="40" height="40" fill="blue"/>'))
check('group-visibility-allows-explicit-visible-child',group.getpixel((20,20))==(0,255,0,255));check('display-none-branch-remains-absent',group.getpixel((70,20))==(0,0,0,0));check('display-none-keeps-next-sibling',group.getpixel((140,20))==(0,0,255,255))
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks,'reference_notes':'Display does not remove gradient color stops. Visibility on an image suppresses that image as a whole; visibility on a group permits visible descendants. Browser pixel readbacks are saved separately.'}
(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2))
(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>可见性资源边界核对</title><style>body{font:19px system-ui;margin:24px}img{width:600px;max-width:100%;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>渐变色标与隐藏图片的真实输出</h1><p>第一幅应与第二幅红蓝渐变一致。第三幅隐藏图片区域应透明，仅保留蓝色兄弟；第四幅可见图片应显示红块。普通组的显式可见孩子仍应呈绿色，display:none分支应消失。</p>'+''.join('<h2>'+n+'</h2><h3>SVG输入</h3><img src="'+n+'.svg"><h3>本次PNG</h3><img src="'+n+'.png"><details><summary>SVG源码</summary><pre>'+html.escape((OUT/(n+'.svg')).read_text())+'</pre></details>' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>')
sys.exit(bool(r['failed']))
