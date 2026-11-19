"""Independent subprocess orchestration pinned to the frozen source path."""
from pathlib import Path
import json,os,subprocess,sys,tempfile
SOURCE=Path(sys.argv[1]).resolve()
BOOT='import sys;sys.path.insert(0,sys.argv.pop(1));from huey.tests import durable_revoke_driver as d;raise SystemExit(d.main(sys.argv))'
def main():
 with tempfile.TemporaryDirectory(prefix='13862-independent-') as tmp:
  env={**os.environ,'REVOKE_DB':str(Path(tmp)/'queue.db'),'PYTHONDONTWRITEBYTECODE':'1'}
  receipts=[]
  def call(mode,*args):
   p=subprocess.run([sys.executable,'-I','-c',BOOT,str(SOURCE),mode,*args],env=env,text=True,capture_output=True,timeout=20)
   assert p.returncode==0,(mode,p.stderr)
   receipts.append({'mode':mode,'stdout':p.stdout.strip(),'exit_code':p.returncode})
   return dict(x.split('=',1) for x in p.stdout.strip().split()) if p.stdout.strip() else {}
  first=call('setup','3');ids=first['ids'].split(',')
  call('revoke-and-exit',ids[0]);check=call('drain');assert check['skipped']==ids[0] and set(check['ran'].split(','))==set(ids[1:])
  first=call('setup','1');ident=first['ids'];call('revoke-and-exit',ident);rest=call('restore-ids',ident);assert rest['restored']=='True';assert call('drain')['ran']==ident
  first=call('setup-failing','1');check=call('race-revoke-and-retry',first['ids']);assert check=={'queue':'0','schedule':'0'}
  print(json.dumps({'source':str(SOURCE),'independent_processes':len(receipts),'persistent_cancel':True,'restore_after_restart':True,'cancel_retry_race':True,'receipts':receipts},ensure_ascii=False))
if __name__=='__main__':main()
