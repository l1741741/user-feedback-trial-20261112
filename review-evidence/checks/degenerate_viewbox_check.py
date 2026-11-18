"""Check actual root/nested output for zero-area viewBoxes against browser rendering."""
from pathlib import Path
from io import BytesIO
import html,json,sys
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
for label,vb in [('zero-width','0 0 0 100'),('zero-height','0 0 100 0'),('zero-both','0 0 0 0')]:
 for nested in [False,True]:
  name=label+('-nested' if nested else '-root');names.append(name)
  inner='<rect x="5" y="5" width="30" height="30" fill="red"/>'
  text=('<svg xmlns="http://www.w3.org/2000/svg" width="160" height="100"><svg x="10" y="10" width="60" height="60" viewBox="'+vb+'">'+inner+'</svg><rect x="100" y="10" width="40" height="40" fill="blue"/></svg>') if nested else '<svg xmlns="http://www.w3.org/2000/svg" width="160" height="100" viewBox="'+vb+'">'+inner+'</svg>'
  (OUT/(name+'.svg')).write_text(text);png=cairosvg.svg2png(bytestring=text.encode());(OUT/(name+'.png')).write_bytes(png);im=Image.open(BytesIO(png)).convert('RGBA');point=(20,20) if nested else (10,10);actual=im.getpixel(point)
  checks.append({'name':name+'-zero-viewbox-suppresses-content','point':point,'actual':actual,'expected':[0,0,0,0],'passed':actual==(0,0,0,0)})
  if nested:checks.append({'name':name+'-blue-sibling','passed':im.getpixel((120,30))==(0,0,255,255)})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks}
(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>零面积视框输出</title><style>body{font:19px system-ui;margin:24px}img{width:480px;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>零面积viewBox应停止该视口内容绘制</h1><p>左侧不应出现红块，嵌套视口右侧蓝色兄弟应保持正常。</p>'+''.join('<h2>'+n+'</h2><h3>SVG输入</h3><img src="'+n+'.svg"><h3>本次PNG</h3><img src="'+n+'.png"><pre>'+html.escape((OUT/(n+'.svg')).read_text())+'</pre>' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
