import tempfile,os,json
from huey import MemoryHuey,SqliteHuey
from huey.api import Task
from huey.exceptions import ConfigurationError

def scenario(h):
    lock=h.lock_task('invoice'); lock.acquire()
    # Queue names and task ids are supported caller input. This id resolves to
    # exactly the physical control key used by the lock for queue r:tenant.
    taskid='tenant.lock.invoice'
    h.revoke_by_id(taskid)
    assert h.is_revoked(Task(id=taskid))
    h.restore_by_id(taskid)
    survives=h.is_locked('invoice')
    print(json.dumps({'backend':type(h).__name__,'queue':h.name,'task_id':taskid,'lock_survives_unrelated_restore':survives}),flush=True)
    return survives
with tempfile.TemporaryDirectory(prefix='keyspace-controls-') as d:
    memory=scenario(MemoryHuey('r:tenant',keyspace_isolation=True))
    sql=SqliteHuey('r:tenant',filename=os.path.join(d,'queue.db'),keyspace_isolation=True)
    sqlite=scenario(sql);sql.storage.close()
    assert memory and sqlite, 'restoring a task deleted an unrelated active task lock'
