"""Independent real-worker boundary check against the unmodified frozen commit."""
import json,multiprocessing as mp,os,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from huey import SqliteHuey,chord
from huey.utils import Error

def build(db,started=None,release=None):
 h=SqliteHuey('document-batch',filename=db,durable_chord=True)
 @h.task(name='review_document')
 def document(value):
  if started is not None:
   started.set()
   if not release.wait(15):raise RuntimeError('test barrier timeout')
  return value
 @h.task(name='review_collect')
 def collect(values):return values
 return h,document,collect

def worker(db,started,release):
 h,_,_=build(db,started,release)
 task=h.dequeue()
 h.execute(task)
 h.storage.close()

def main():
 ctx=mp.get_context('fork')
 with tempfile.TemporaryDirectory(prefix='13863-inflight-') as tmp:
  db=str(Path(tmp)/'queue.db');h,document,collect=build(db)
  # Positive control: normal completion retains input order.
  ok=h.enqueue(chord([document.s(11),document.s(22)],collect.s()))
  while h.pending_count():h.execute(h.dequeue())
  assert ok()==[11,22]
  print(json.dumps({'normal_ordered_result':[11,22]},ensure_ascii=False),flush=True)
  result=h.enqueue(chord([document.s(42)],collect.s()))
  started=ctx.Event();release=ctx.Event();p=ctx.Process(target=worker,args=(db,started,release));p.start()
  try:
   assert started.wait(8),'worker failed to start'
   assert p.is_alive() and h.pending_count()==0
   # Explicit supported configuration, actual elapsed time, no source patch.
   time.sleep(.25)
   sweep=h.chord_sweep(grace=.1,retention=10**9)
   callback_queued=h.pending_count()
   early_callback=h.dequeue()
   if early_callback is not None:h.execute(early_callback)
   value=result.get(preserve=True) if early_callback is not None else None
   evidence={'worker_alive_at_sweep':p.is_alive(),'grace_seconds':.1,'callback_queued_before_member_finished':callback_queued,'premature_error':bool(isinstance(value,list) and value and isinstance(value[0],Error)),'sweep':sweep}
   release.set();p.join(8)
   evidence.update(worker_exit_code=p.exitcode,member_result=result.results.get(preserve=True),callback_after_real_completion=h.pending_count())
   print(json.dumps(evidence,ensure_ascii=False,default=str),flush=True)
   assert callback_queued==0,'A live member must not be finalized as lost or trigger the callback'
  finally:
   release.set();p.join(2)
   if p.is_alive():p.terminate();p.join()
   h.storage.close()
if __name__=='__main__':main()
