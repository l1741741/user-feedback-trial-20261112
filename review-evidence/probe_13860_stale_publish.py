import datetime, tempfile, os, json
from huey import SqliteHuey
with tempfile.TemporaryDirectory(prefix='handoff-aba-') as d:
    path=os.path.join(d,'queue.db'); now=datetime.datetime.now()
    first=SqliteHuey('dispatch',filename=path,reliable_schedule=True,utc=False)
    second=SqliteHuey('dispatch',filename=path,reliable_schedule=True,utc=False)
    first.storage.add_to_schedule(b'first-task',now)
    stale_id,stale_payload=first.storage.claim_scheduled(now)[0]
    # Another scheduler completes the same durable row while the first is paused.
    assert second.storage.publish_scheduled(stale_id,stale_payload,0)
    # A later due task enters the handoff while the first scheduler is still paused.
    second.storage.add_to_schedule(b'new-task',now)
    new_id,new_payload=second.storage.claim_scheduled(now)[0]
    stale_accepted=first.storage.publish_scheduled(stale_id,stale_payload,0)
    new_accepted=second.storage.publish_scheduled(new_id,new_payload,0)
    got=second.storage.enqueued_items()
    print(json.dumps({'old_handoff_id':stale_id,'new_handoff_id':new_id,'stale_accepted':stale_accepted,'new_accepted':new_accepted,'actual_queue':[x.decode() for x in got],'remaining_handoff':second.storage.handoff_size()},ensure_ascii=False),flush=True)
    first.storage.close(); second.storage.close()
    assert sorted(got)==[b'first-task',b'new-task'], 'stale publisher duplicated old task and removed the later handoff'
