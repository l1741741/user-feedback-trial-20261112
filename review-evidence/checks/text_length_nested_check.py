from pathlib import Path
from io import BytesIO
import json,sys,html
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
def render(name,body):
 names.append(name);s='<svg xmlns="http://www.w3.org/2000/svg" width="400" height="100"><text x="20" y="65" font-family="Menlo" font-size="36" fill="red" '+body+'</text><rect x="350" y="20" width="30" height="50" fill="blue"/></svg>'
 (OUT/(name+'.svg')).write_text(s);png=cairosvg.svg2png(bytestring=s.encode());(OUT/(name+'.png')).write_bytes(png);im=Image.open(BytesIO(png)).convert('RGBA');return im
for mode in ['spacing','spacingAndGlyphs']:
 ref=render(mode+'-flat',f'textLength="240" lengthAdjust="{mode}">ABCD')
 for label,text in [('empty-parent','<tspan>ABCD</tspan>'),('mixed','AB<tspan>CD</tspan>')]:
  im=render(mode+'-'+label,f'textLength="240" lengthAdjust="{mode}">'+text)
  def bbox(i):
   mask=i.getchannel('R').point(lambda x:255 if x>128 else 0);return mask.getbbox()
  checks.append({'name':mode+'-'+label+'-same-target-as-flat','passed':im.tobytes()==ref.tobytes(),'actual_red_bbox':bbox(im),'reference_red_bbox':bbox(ref),'changed_pixels':sum(a!=b for a,b in zip(im.getdata(),ref.getdata()))})
  checks.append({'name':mode+'-'+label+'-blue-sibling','passed':im.getpixel((360,40))==(0,0,255,255)})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>嵌套文本目标宽度</title><style>body{font:20px system-ui;margin:24px}img{width:1000px;max-width:100%;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>240单位目标宽度：完整文本与等价tspan</h1><p>同字体、字号和内容，增加无样式tspan不应改变目标宽度；蓝色兄弟保持正常。</p>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
