"""
Async client for the local inventory service.

One client holds one TCP connection. A client is created per consumer
worker (in an ``on_startup`` hook) and reused by every async task that
worker executes, so consecutive tasks on the same worker share the
worker's connection while different workers never share one.
"""
import asyncio


class InventoryClient(object):
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.reader = None
        self.writer = None
        self.closed = False
        self.last_conn_id = None

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(
            self.host, self.port)

    async def fetch(self, sku):
        if self.closed:
            raise RuntimeError('client is closed')
        self.writer.write(b'fetch %s\n' % sku.encode('utf-8'))
        await self.writer.drain()
        line = await self.reader.readline()
        if not line:
            raise ConnectionError('inventory service closed the connection')
        conn_id, resp_sku, stock = line.decode('utf-8').split()
        self.last_conn_id = int(conn_id)
        return {
            'conn_id': int(conn_id),
            'sku': resp_sku,
            'stock': int(stock),
        }

    async def close(self):
        self.closed = True
        if self.writer is not None:
            self.writer.close()
            await self.writer.wait_closed()
