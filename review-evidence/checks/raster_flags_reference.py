from pathlib import Path
import io,sys,json,html,hashlib
import cairosvg
from PIL import Image
OUT=Path(sys.argv[-1]);OUT.mkdir(parents=True,exist_ok=True);assets=OUT/'assets';assets.mkdir(exist_ok=True);checks=[];names=[]
Image.new('RGBA',(40,40),(255,0,0,128)).save(assets/'rgba.png')
p=Image.new('P',(40,40));palette=[0]*768;palette[3:6]=[0,255,0];p.putpalette(palette)
for x in range(20,40):
 for y in range(40):p.putpixel((x,y),1)
p.info['transparency']=bytes([0,128]);p.save(assets/'palette.png')
Image.new('RGB',(40,40),(255,0,0)).save(assets/'rgb.png')
im=Image.new('RGB',(40,20),(255,0,0))
for x in range(20,40):
 for y in range(20):im.putpixel((x,y),(0,0,255))
ex=Image.Exif();ex[274]=6;im.save(assets/'oriented.png',exif=ex)
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();before={str(p):sha(p) for p in assets.iterdir()};text='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200"><rect x="10" y="10" width="30" height="30" fill="red"/><image href="assets/rgba.png" x="10" y="60" width="40" height="40"/><image href="assets/palette.png" x="70" y="60" width="40" height="40"/><image href="assets/rgb.png" x="130" y="60" width="40" height="40"/><image href="assets/oriented.png" x="200" y="60" width="20" height="40"/></svg>'
for name,neg,inv in [('off',False,False),('negate',True,False),('invert',False,True),('both',True,True)]:
 svg=OUT/(name+'.svg');svg.write_text(text);data=cairosvg.svg2png(url=str(svg),negate_colors=neg,invert_images=inv);(OUT/(name+'.png')).write_bytes(data);names.append(name);im=Image.open(io.BytesIO(data)).convert('RGBA')
 expected=[('vector',(20,20),((0,255,255,255) if neg else (255,0,0,255))),('rgba',(20,70),((0,255,255,128) if inv else (255,0,0,128))),('palette-transparent',(75,70),(0,0,0,0)),('palette-alpha',(100,70),((255,0,255,128) if inv else (0,255,0,128))),('rgb',(140,70),((0,255,255,255) if inv else (255,0,0,255))),('orientation-top',(210,65),((0,255,255,255) if inv else (255,0,0,255))),('orientation-bottom',(210,95),((255,255,0,255) if inv else (0,0,255,255)))]
 for label,point,want in expected:
  actual=im.getpixel(point);checks.append({'name':name+'-'+label,'actual':actual,'expected':want,'passed':all(abs(x-y)<=1 for x,y in zip(actual,want))})
 if name=='off':original=data
again=cairosvg.svg2png(url=str(OUT/'off.svg'));checks.append({'name':'off-after-other-options-identical','passed':again==original});checks.append({'name':'local-input-bytes-unchanged','passed':all(sha(Path(p))==h for p,h in before.items())})
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>矢量与本地栅格反色</title><style>body{font:20px system-ui;margin:24px}img{width:720px;max-width:100%;border:1px solid #aaa;background:repeating-conic-gradient(#eee 0% 25%,#fff 0% 50%) 50%/20px 20px}pre{white-space:pre-wrap}</style><h1>300×200本地素材版式及四种开关</h1><p>矢量红块、半透明RGBA、透明调色板、RGB和带EXIF方向的PNG；上红下蓝的方向在各开关保持。</p>'+''.join('<h2>'+n+'</h2><p>SVG输入</p><img src="'+n+'.svg"><p>当次PNG</p><img src="'+n+'.png">' for n in names)+'<pre>'+html.escape(json.dumps(r,ensure_ascii=False,indent=2))+'</pre>');sys.exit(bool(r['failed']))
