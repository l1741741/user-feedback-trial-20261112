"""
Local inventory service used to demonstrate Huey's async consumer.

A tiny asyncio TCP server. Each accepted connection is assigned an
incrementing connection id, which is included in every response so that
clients (and tests) can tell whether a connection is being reused.

Protocol (line-based):

    request:  "fetch <sku>\\n"
    response: "<conn_id> <sku> <stock>\\n"

Run standalone with: python -m examples.async_inventory.service [port]
"""
import asyncio
import sys
import threading


class InventoryService(object):
    def __init__(self, host='127.0.0.1', port=0):
        self.host = host
        self.port = port
        self.events = []  # ('connect'|'disconnect', conn_id) in order.
        self._next_conn_id = 0
        self._loop = None
        self._server = None
        self._thread = None

    def stock_for(self, conn_id, sku):
        # Deterministic "inventory level" for the demo.
        return (conn_id * 13 + sum(sku.encode('utf-8'))) % 50

    async def handle_connection(self, reader, writer):
        self._next_conn_id += 1
        conn_id = self._next_conn_id
        self.events.append(('connect', conn_id))
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # Client closed the connection.
                parts = line.decode('utf-8').split()
                if len(parts) != 2 or parts[0] != 'fetch':
                    writer.write(b'error\n')
                else:
                    sku = parts[1]
                    writer.write(b'%d %s %d\n' % (
                        conn_id, sku.encode('utf-8'),
                        self.stock_for(conn_id, sku)))
                await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            self.events.append(('disconnect', conn_id))
            writer.close()

    def connection_events(self, kind):
        return [conn_id for event, conn_id in self.events if event == kind]

    def start_in_thread(self):
        """
        Run the service in a background thread. Returns the thread. The
        bound port is available on ``self.port`` once this returns.
        """
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def main():
                self._server = await asyncio.start_server(
                    self.handle_connection, self.host, self.port)
                self.port = self._server.sockets[0].getsockname()[1]
                ready.set()
                async with self._server:
                    await self._server.serve_forever()

            try:
                self._loop.run_until_complete(main())
            except asyncio.CancelledError:
                pass  # Server closed via stop().
            finally:
                self._loop.close()

        self._thread = threading.Thread(target=run, name='InventoryService',
                                        daemon=True)
        self._thread.start()
        if not ready.wait(10):
            raise RuntimeError('inventory service failed to start')
        return self._thread

    def stop(self):
        if self._loop is not None and self._server is not None:
            # Closing the server cancels serve_forever(), ending the thread.
            self._loop.call_soon_threadsafe(self._server.close)
        if self._thread is not None:
            self._thread.join(10)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 13771
    service = InventoryService(port=port)
    service.start_in_thread()
    print('inventory service listening on 127.0.0.1:%d' % service.port)
    try:
        service._thread.join()
    except KeyboardInterrupt:
        service.stop()


if __name__ == '__main__':
    main()
