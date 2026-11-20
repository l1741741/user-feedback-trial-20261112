.. _asyncio:

AsyncIO
-------

Huey provides two helpers for ``await``-ing task results, which poll the
storage backend until the result is ready. For a complete example of using
Huey in an async web application, see :ref:`recipe-fastapi`.

Additionally, the thread-based consumer can execute ``async def`` tasks
natively, reusing per-worker async resources across tasks -- see
:ref:`async-tasks`.

.. py:function:: aget_result(res, backoff=1.15, max_delay=1.0, preserve=False, timeout=None)

    :param Result res: a result handle returned when calling a task.
    :param timeout: seconds to wait before raising :py:class:`ResultTimeout`.
    :return: task return value.

    Example:

    .. code-block:: python

        @huey.task()
        def sleep(n):
            time.sleep(n)
            return n

        async def main():
            # Single task, will finish in ~2 seconds (other coroutines can run
            # during this time!).
            rh = sleep(2)
            result = await aget_result(rh)

            # Awaiting multiple results. This will also finish in ~2 seconds.
            r1 = sleep(2)
            r2 = sleep(2)
            r3 = sleep(2)
            results = await asyncio.gather(
                aget_result(r1),
                aget_result(r2),
                aget_result(r3))

            # Give up after 5 seconds.
            try:
                result = await aget_result(sleep(10), timeout=5)
            except ResultTimeout:
                ...


.. py:function:: aget_result_group(rg, *args, **kwargs)

    :param ResultGroup rg: a result-group handle for multiple tasks.
    :return: return values for all tasks in the result group.

    Example:

    .. code-block:: python

        @huey.task()
        def sleep(n):
            time.sleep(n)
            return n

        async def main():
            # Spawn 3 "sleep" tasks, each sleeping for 2 seconds.
            rg = sleep.map([2, 2, 2])

            # Await the results. This will finish in ~2 seconds while also
            # allowing other coroutines to run.
            results = await aget_result_group(rg)

.. _async-tasks:

Executing coroutine tasks
-------------------------

The thread-based consumer can execute ``async def`` tasks natively. Opt in by
passing ``enable_async=True`` when creating the consumer, or by using the
``-e`` / ``--enable-async`` command-line flag:

.. code-block:: python

    consumer = huey.create_consumer(enable_async=True)

.. code-block:: shell

    huey_consumer my.app.huey -e

Each worker thread runs its own asyncio event-loop, which is reused for every
coroutine task that worker executes. Async resources created by
:py:meth:`~Huey.on_startup` hooks (which may themselves be coroutine
functions) therefore live for the lifetime of the worker and are shared by
consecutive tasks on that worker -- but never shared between workers:

.. code-block:: python

    import threading
    from huey import SqliteHuey

    huey = SqliteHuey('my-app', filename='queue.db')
    _state = threading.local()

    @huey.on_startup()
    async def connect_client():
        # Runs once per worker, on that worker's own event-loop.
        _state.client = await AsyncClient.connect()

    @huey.on_shutdown()
    async def close_client():
        await _state.client.close()

    @huey.task()
    async def fetch_stock(sku):
        return await _state.client.fetch(sku)

Some things to note:

* Coroutine tasks return values, raise errors, and are retried exactly like
  regular tasks. The coroutine is awaited before its result is stored, so
  result handles, pipelines (``.then()``) and chords all work unchanged, and
  synchronous tasks can be mixed in freely.
* Async task support requires the default ``thread`` worker-type. Combining
  ``enable_async`` with the ``process`` or ``greenlet`` worker-types raises
  :py:class:`ConfigurationError`.
* A coroutine task dequeued by a consumer that does not have async support
  enabled fails with a :py:class:`ConfigurationError`, rather than storing
  the un-awaited coroutine object as the task result.
* On shutdown, in-flight tasks finish as usual, then
  :py:meth:`~Huey.on_shutdown` hooks (which may be coroutine functions) run
  and each worker's event-loop is closed. If a startup hook fails, the worker
  cleans up any partially-initialized resources and exits, rather than
  running in a half-initialized state.

A complete example using a local service and a real consumer process can be
found in ``examples/async_inventory``.
