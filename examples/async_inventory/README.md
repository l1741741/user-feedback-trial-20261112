# Async consumer demo: inventory sync with connection reuse

Demonstrates running coroutine tasks natively on Huey's thread consumer.
Each worker owns an asyncio event loop and one `InventoryClient`
connection; consecutive tasks on the same worker reuse that connection,
and the two workers never share one.

Files:

- `service.py` — local TCP "inventory service". Assigns each connection an
  id and answers `fetch <sku>` with `<conn_id> <sku> <stock>`.
- `client.py` — async `InventoryClient`, one TCP connection per instance.
- `tasks.py` — `SqliteHuey` instance, async startup/shutdown hooks that
  open/close the per-worker client, and the demo tasks.
- `consumer.py` — consumer entry-point using `enable_async=True`.

Run it manually from the repository root:

```sh
# Terminal 1: start the local inventory service on the default port.
python -m examples.async_inventory.service 13771

# Terminal 2: start a 2-worker consumer with async task support.
python -m examples.async_inventory.consumer 2

# Terminal 3: enqueue some work.
python - <<'EOF'
from examples.async_inventory.tasks import fetch_stock, huey
results = [fetch_stock('sku-%d' % i) for i in range(6)]
for r in results:
    print(r.get(blocking=True, timeout=30))
EOF
```

Each result includes the worker name and the service-side connection id:
one connection id per worker, reused across all of that worker's tasks.
Stop the consumer with Ctrl-C (SIGINT) and the shutdown hook closes each
worker's client before its event loop is torn down; the service logs the
disconnects.

The same scenario is exercised end-to-end (with a real consumer process)
by `huey/tests/test_async_consumer.py`.
