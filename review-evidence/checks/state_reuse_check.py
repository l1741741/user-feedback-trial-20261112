from pathlib import Path
from io import BytesIO
import json,sys,traceback
import cairosvg
from cairosvg.parser import Tree
from cairosvg.surface import PNGSurface
from PIL import Image
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True);checks=[]
def test(name,fn):
 try:detail=fn();checks.append({'name':name,'passed':True,'details':detail})
 except Exception as e:checks.append({'name':name,'passed':False,'error':str(e),'traceback':traceback.format_exc()})
def doc(body):return '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160">'+body+'</svg>'
safe='<rect x="150" y="20" width="60" height="60" fill="blue"/>'
(OUT/'reference.svg').write_text(doc(safe));reference=cairosvg.svg2png(bytestring=doc(safe).encode());(OUT/'reference.png').write_bytes(reference)
def failure_reuse():
 output=BytesIO();surface=PNGSurface(Tree(bytestring=doc('').encode()),output,96)
 before=tuple(surface.context.get_matrix());bad=doc('<defs><clipPath id="c"><rect width="20" height="20"/></clipPath></defs><g transform="translate(40 40)" opacity=".5" clip-path="url(#c)"><rect width="50" height="50" stroke-miterlimit="not-a-number" fill="red"/></g>');(OUT/'bad-layer.svg').write_text(bad)
 try:surface.draw(Tree(bytestring=bad.encode()))
 except ValueError:pass
 else:raise AssertionError('Existing numeric parse error was silently swallowed')
 after=tuple(surface.context.get_matrix());assert before==after,(before,after)
 surface.draw(Tree(bytestring=doc(safe).encode()));surface.finish();data=output.getvalue();(OUT/'after-failure.png').write_bytes(data)
 a=Image.open(BytesIO(data)).convert('RGBA');b=Image.open(BytesIO(reference)).convert('RGBA');assert a.tobytes()==b.tobytes(),'Failure polluted the reused renderer or its blue sibling'
 return {'before_matrix':before,'after_matrix':after,'same_pixels_as_reference':True}
test('actual-draw-error-propagates-and-same-renderer-recovers',failure_reuse)
pattern=doc('<defs><pattern id="p" width="20" height="20" patternUnits="userSpaceOnUse"><rect width="10" height="20" fill="red"/></pattern></defs><rect x="10" y="10" width="80" height="60" fill="url(#p)" fill-opacity=".5"/><rect x="120" y="10" width="80" height="60" fill="url(#p)"/>')
def shared_tree():
 tree=Tree(bytestring=pattern.encode());outputs=[]
 for n in ['first-tree-render','second-tree-render']:
  out=BytesIO();surface=PNGSurface(tree,out,96);surface.finish();data=out.getvalue();(OUT/(n+'.png')).write_bytes(data);outputs.append(Image.open(BytesIO(data)).convert('RGBA'))
 (OUT/'shared-pattern.svg').write_text(pattern);assert outputs[0].tobytes()==outputs[1].tobytes(),'Caller tree changed between exports'
 assert outputs[0].getpixel((125,30))==(255,0,0,255),outputs[0].getpixel((125,30))
 assert 125<=outputs[0].getpixel((25,30))[3]<=130,outputs[0].getpixel((25,30))
 return {'repeat_same_pixels':True,'half_alpha':outputs[0].getpixel((25,30))[3],'full_alpha':outputs[0].getpixel((125,30))[3]}
test('shared-pattern-tree-repeat-and-opacity-isolation',shared_tree)
r={'source':cairosvg.__file__,'tests':len(checks),'passed':sum(x['passed'] for x in checks),'failed':sum(not x['passed'] for x in checks),'checks':checks};(OUT/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False,indent=2));(OUT/'gallery.html').write_text('<!doctype html><meta charset="utf-8"><title>真实异常后绘制隔离</title><style>body{font:19px system-ui;margin:24px}section{display:inline-block;margin:10px;padding:12px;background:#eee}img{width:480px;max-width:100%;border:1px solid #999;image-rendering:pixelated}pre{white-space:pre-wrap}</style><h1>同一绘制入口在真实错误之后恢复</h1><p>实际错误需向外抛出，之后的蓝色图层应与独立基准一致。共享图案的两次绘制应相同，后一对象不累乘前一对象透明度。</p>'+''.join('<section><h2>'+n+'</h2><img src="'+n+'.png"></section>' for n in ['reference','after-failure','first-tree-render','second-tree-render'] if (OUT/(n+'.png')).exists())+'<pre>'+json.dumps(r,ensure_ascii=False,indent=2)+'</pre>');sys.exit(bool(r['failed']))
