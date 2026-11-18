from pathlib import Path
from io import BytesIO
import json,sys
import cairosvg
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[]
for name,offset,anchor in [('negative','-100%','start'),('past-end','200%','end')]:
 svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="240" height="120"><defs><path id="p" d="M20 60H180"/></defs><text fill="red" font-family="DejaVu Sans" font-size="20" text-anchor="{anchor}"><textPath href="#p" startOffset="{offset}">MMMM</textPath></text><rect x="210" y="20" width="20" height="60" fill="blue"/></svg>'
 (OUT/(name+'.svg')).write_text(svg);data=cairosvg.svg2png(bytestring=svg.encode());(OUT/(name+'.png')).write_bytes(data);im=Image.open(BytesIO(data)).convert('RGBA');red_pixels=sum(a>0 and r>200 and g<30 and b<30 for r,g,b,a in im.getdata());checks.append({'name':name+'-glyphs-outside-path-omitted','expected_red_pixels':0,'actual_red_pixels':red_pixels,'passed':red_pixels==0});checks.append({'name':name+'-sibling-blue','actual':list(im.getpixel((220,50))),'expected':[0,0,255,255],'passed':im.getpixel((220,50))==(0,0,255,255)})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(c['passed'] for c in checks),'failed':sum(not c['passed'] for c in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>路径偏移边界核验</title><style>body{font:20px system-ui;margin:24px}img{width:600px;border:1px solid #888;background:white;image-rendering:pixelated}</style><h1>路径外字符应省略，蓝色兄弟应保留</h1>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>本次PNG</p><img src="'+n+'.png">' for n in ['negative','past-end'])+'<pre>'+json.dumps(r,ensure_ascii=False,indent=2)+'</pre>');sys.exit(bool(r['failed']))
