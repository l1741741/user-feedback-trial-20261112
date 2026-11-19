import os, tempfile, json
from huey import MemoryHuey, SqliteHuey
from huey.api import Task

def check(h):
    key='same-literal'; h.put(key,'business'); h.put_result(key,'result'); h.revoke_by_id(key)
    lock=h.lock_task(key); assert lock.acquire() is True
    rate=h.rate_limit(key,5,60); rate.acquire()
    assert h.get(key,peek=True)=='business'; assert h.result(key,preserve=True)=='result'
    assert h.is_revoked(Task(id=key)); assert h.is_locked(key); assert rate.current_usage()==1
    h.storage.flush_results()
    assert h.get(key,peek=True)=='business'; assert h.is_revoked(Task(id=key)); assert h.is_locked(key); assert rate.current_usage()==1
    h.storage.flush_control()
    assert not h.is_revoked(Task(id=key)); assert not h.is_locked(key); assert h.get(key,peek=True)=='business'
    h.delete(key); assert h.get(key,peek=True) is None

check(MemoryHuey('mem',keyspace_isolation=True))
fd,path=tempfile.mkstemp(prefix='accept-keyspace-',suffix='.db');os.close(fd);os.unlink(path)
h=SqliteHuey('one',filename=path,keyspace_isolation=True);check(h);h.storage.close()
h=SqliteHuey('one',filename=path,keyspace_isolation=True);assert h.get('same-literal',peek=True) is None;h.put('same-literal','one');h.storage.close()
h2=SqliteHuey('two',filename=path,keyspace_isolation=True);assert h2.get('same-literal',peek=True) is None;h2.put('same-literal','two');assert h2.get('same-literal',peek=True)=='two';h2.storage.close()
for suffix in ('','-wal','-shm'):
 try:os.unlink(path+suffix)
 except FileNotFoundError:pass
print(json.dumps({'memory_planes_isolated':True,'sqlite_planes_isolated':True,'flush_scoped':True,'queues_isolated':True}))
