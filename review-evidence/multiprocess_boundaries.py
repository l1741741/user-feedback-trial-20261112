"""Real process persistence and stale-operation boundary checks on frozen sources."""
import sys, subprocess, os, tempfile, json, datetime
from pathlib import Path

mode=sys.argv[1]
source=Path(sys.argv[2]).resolve()
code=''
with tempfile.TemporaryDirectory(prefix='middleware-process-check-') as d:
    db=str(Path(d)/'queue.db')
    env={**os.environ,'PYTHONPATH':str(source),'PYTHONDONTWRITEBYTECODE':'1'}
    def run(text):
        result=subprocess.run([sys.executable,'-c',text,db],env=env,capture_output=True,text=True,timeout=20)
        if result.returncode: raise RuntimeError(result.stdout+result.stderr)
        return json.loads(result.stdout)
    if mode=='handoff':
        setup="""import sys,json,datetime
from huey import SqliteHuey
h=SqliteHuey('dispatch',filename=sys.argv[1],reliable_schedule=True,utc=False)
h.storage.add_to_schedule(b'first',datetime.datetime.now())
a=h.storage.claim_scheduled(datetime.datetime.now())[0]
print(json.dumps({'handoff_id':a[0],'data':a[1].decode()}));h.storage.close()
"""
        old=run(setup)
        publish="""import sys,json,datetime
from huey import SqliteHuey
h=SqliteHuey('dispatch',filename=sys.argv[1],reliable_schedule=True,utc=False)
a=h.storage.claim_scheduled(datetime.datetime.now())[0]
assert h.storage.publish_scheduled(*a)
h.storage.add_to_schedule(b'next',datetime.datetime.now())
b=h.storage.claim_scheduled(datetime.datetime.now())[0]
print(json.dumps({'new_handoff_id':b[0],'data':b[1].decode()}));h.storage.close()
"""
        new=run(publish)
        stale=run("""import sys,json
from huey import SqliteHuey
h=SqliteHuey('dispatch',filename=sys.argv[1],reliable_schedule=True,utc=False)
old_id="""+repr(old['handoff_id'])+"""
old_data="""+repr(old['data'].encode())+"""
accepted=h.storage.publish_scheduled(old_id,old_data)
print(json.dumps({'stale_accepted':accepted,'queue':[x.decode() for x in h.storage.enqueued_items()],'handoff_left':h.storage.handoff_size()}));h.storage.close()
""")
        observed={'processes':3,'old':old,'new':new,'result':stale}
        print(json.dumps(observed),flush=True)
        assert not stale['stale_accepted'] and stale['handoff_left']==1, 'stale operation consumed later durable handoff after process restart'
    elif mode=='keyspace':
        first=run("""import sys,json
from huey import SqliteHuey
h=SqliteHuey('r:tenant',filename=sys.argv[1],keyspace_isolation=True)
h.lock_task('invoice').acquire();h.put('unrelated','retained')
print(json.dumps({'lock':h.is_locked('invoice')}));h.storage.close()
""")
        second=run("""import sys,json
from huey import SqliteHuey
h=SqliteHuey('r:tenant',filename=sys.argv[1],keyspace_isolation=True)
h.revoke_by_id('tenant.lock.invoice');h.restore_by_id('tenant.lock.invoice')
print(json.dumps({'restore_returned':True}));h.storage.close()
""")
        last=run("""import sys,json
from huey import SqliteHuey
h=SqliteHuey('r:tenant',filename=sys.argv[1],keyspace_isolation=True)
print(json.dumps({'lock':h.is_locked('invoice'),'business':h.get('unrelated',peek=True)}));h.storage.close()
""")
        print(json.dumps({'processes':3,'before':first,'restore':second,'after':last}),flush=True)
        assert last['lock'] is True, 'task restore removed independently acquired lock across real process restarts'
    else: raise ValueError(mode)
