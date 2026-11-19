import datetime, os, tempfile, json
from huey import SqliteHuey, MemoryHuey
from huey.exceptions import ConfigurationError

def main():
    fd, path = tempfile.mkstemp(prefix='accept-handoff-', suffix='.db'); os.close(fd); os.unlink(path)
    now = datetime.datetime.now(); future = now + datetime.timedelta(hours=1)
    h = SqliteHuey('accept', filename=path, reliable_schedule=True, utc=False)
    try:
        h.storage.add_to_schedule(b'due', now - datetime.timedelta(seconds=1))
        h.storage.add_to_schedule(b'future', future)
        claimed = h.storage.claim_scheduled(now)
        assert [x[1] for x in claimed] == [b'due']
        assert h.storage.schedule_size() == 1 and h.storage.handoff_size() == 1
        hid, data = claimed[0]
        assert h.storage.publish_scheduled(hid, data, 7) is True
        assert h.storage.queue_size() == 1 and h.storage.handoff_size() == 0
        assert h.storage.publish_scheduled(hid, data, 7) is False
        assert h.storage.queue_size() == 1
        other = SqliteHuey('accept', filename=path, reliable_schedule=True, utc=False)
        try:
            assert other.storage.publish_scheduled(hid, data, 7) is False
            assert other.storage.queue_size() == 1
        finally: other.storage.close()
    finally:
        h.storage.close()
    for suffix in ('', '-wal', '-shm'):
        try: os.unlink(path + suffix)
        except FileNotFoundError: pass
    try:
        MemoryHuey('unsupported', reliable_schedule=True)
    except ConfigurationError:
        pass
    else: raise AssertionError('unsupported backend was accepted')
    print(json.dumps({'due_claimed':True,'future_preserved':True,'publish_idempotent':True,'dual_scheduler_safe':True,'unsupported_backend_rejected':True}))
main()
