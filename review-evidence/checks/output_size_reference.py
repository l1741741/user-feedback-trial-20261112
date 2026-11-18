from pathlib import Path
from io import BytesIO
import sys,json,html
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[];names=[]
svg='<svg xmlns="http://www.w3.org/2000/svg" width="100" height="50" viewBox="10 20 100 50"><rect x="10" y="20" width="100" height="50" fill="red"/></svg>'
for n,kw,size in [('width-fraction',{'output_width':25.5},(26,13)),('height-fraction',{'output_height':12.6},(26,13)),('scale',{'scale':.26},(26,13)),('both',{'output_width':80,'output_height':40},(80,40)),('tiny',{'output_width':1},(1,1))]:
 names.append(n);(OUT/(n+'.svg')).write_text(svg);p=cairosvg.svg2png(bytestring=svg.encode(),**kw);(OUT/(n+'.png')).write_bytes(p);im=Image.open(BytesIO(p)).convert('RGBA');checks.append({'name':n+'-stored-size','actual':im.size,'expected':size,'passed':im.size==size})
 if n!='tiny':
  corners=[im.getpixel(pt) for pt in [(0,0),(im.width-1,0),(0,im.height-1),(im.width-1,im.height-1)]];checks.append({'name':n+'-opaque-edge-corners','actual':corners,'passed':all(c==(255,0,0,255) for c in corners)})
for name,kw in [('zero',{'output_width':0}),('negative',{'output_height':-1}),('nan',{'output_width':float('nan')}),('infinite',{'output_height':float('inf')})]:
 target=OUT/(name+'-should-not-exist.png');error=None
 try:cairosvg.svg2png(bytestring=svg.encode(),write_to=str(target),**kw)
 except Exception as e:error=type(e).__name__
 checks.append({'name':name+'-reject-before-file','error':error,'file_created':target.exists(),'passed':error=='ValueError' and not target.exists()})
physical='<svg xmlns="http://www.w3.org/2000/svg" width="1in" height=".5in"><rect width="100%" height="100%" fill="blue"/></svg>';names.append('physical-dpi');(OUT/'physical-dpi.svg').write_text(physical);p=cairosvg.svg2png(bytestring=physical.encode(),dpi=120);(OUT/'physical-dpi.png').write_bytes(p);im=Image.open(BytesIO(p)).convert('RGBA');checks.append({'name':'physical-inch-dpi120','passed':im.size==(120,60) and im.getpixel((119,59))==(0,0,255,255)})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>输出尺寸与贴边像素</title><style>body{font:20px system-ui;margin:24px}img{width:600px;image-rendering:pixelated;border:1px solid #aaa;background:#eee}pre{white-space:pre-wrap}</style><h1>小数、单尺寸、scale与物理单位</h1><p>红色贴边矩形四角不应出现透明线；非法尺寸在创建文件前拒绝。</p>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
