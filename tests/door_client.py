"""Drive one request through the HTTP doors, over a real socket.

The app the daemon serves, started on an ephemeral port for the length of one
request, so a route test reads the status and body a client would - never a
handler's return value. Every slice reaches this through the path the root
conftest puts on ``sys.path``."""

from __future__ import annotations

import asyncio
import json
from typing import Any, NamedTuple


class Answer(NamedTuple):
    """What the door answered: the status, the raw body and the headers."""

    status: int
    body: bytes
    headers: dict

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def drive(method: str, path: str, body: bytes | None = None,
          headers: dict | None = None) -> Answer:
    """One request through the whole app - middlewares, router and handler."""
    from aiohttp.test_utils import TestClient, TestServer

    from trid3nt_server.server.protocol.doors import build_app

    async def run() -> Answer:
        client = TestClient(TestServer(build_app()))
        await client.start_server()
        try:
            response = await client.request(method, path, data=body,
                                            headers=headers)
            return Answer(response.status, await response.read(),
                          dict(response.headers))
        finally:
            await client.close()

    return asyncio.run(run())


def drive_raw(raw: bytes) -> Answer:
    """One hand-written request onto the door's socket, for the claims about
    the transport itself - a declared length the body never carries, say. The
    answer is read to its own Content-Length, never to EOF, so a server still
    draining what the request promised does not stall the test."""
    from aiohttp import web

    from trid3nt_server.server.protocol.doors import build_app

    async def run() -> Answer:
        runner = web.AppRunner(build_app(), access_log=None,
                               shutdown_timeout=0.1)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(raw)
            await writer.drain()
            head = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=15)
            lines = head.decode("latin-1").rstrip().split("\r\n")
            headers = dict(
                (k.strip(), v.strip())
                for k, _, v in (ln.partition(":") for ln in lines[1:]) if k
            )
            length = int(headers.get("Content-Length", 0))
            body = await asyncio.wait_for(
                reader.readexactly(length), timeout=15) if length else b""
            writer.close()
        finally:
            await runner.cleanup()
        return Answer(int(lines[0].split(" ")[1]), body, headers)

    return asyncio.run(run())
