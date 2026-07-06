"""Local Unix-domain socket module: a plain command pipe.

Write a command line, read one reply line back::

    echo 'all hsi 240 100 80' | nc -U /run/neewerd.sock

This is the simplest front-end and has no dependencies beyond the stdlib. It's
the default-on module and a good way to script the daemon from the same host.
"""
from __future__ import annotations

import asyncio
import logging
import os

from ..socketpath import default_socket_path

log = logging.getLogger("neewerd.socket")

SOCKET_MODE = 0o660            # owner+group rw; keep the pipe off-limits to others


async def _serve_client(core, reader: asyncio.StreamReader,
                        writer: asyncio.StreamWriter) -> None:
    """One client connection: dispatch every line, reply with the result line.

    Extracted from ``run`` so the command loop is unit-testable with in-memory
    streams (no real socket). A dispatch error is reported to the client as
    ``error: <msg>`` rather than dropping the connection.
    """
    while not reader.at_eof():
        line = await reader.readline()
        if not line:
            break
        try:
            reply = await core.dispatch(line.decode().strip())
        except Exception as exc:
            reply = f"error: {exc}"
        try:
            writer.write((str(reply) + "\n").encode())
            await writer.drain()
        except Exception:
            break
    writer.close()


async def run(core, cfg) -> None:
    """Serve a Unix-domain socket forever, dispatching each received line."""
    # Explicit config wins; otherwise resolve the runtime-dir default (see
    # neewerd.socketpath). The CLI resolves the same path.
    path = cfg.get("path") or default_socket_path()

    # Make sure the parent dir exists (e.g. /run/neewerd from RuntimeDirectory).
    parent = os.path.dirname(path)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            log.warning("could not create socket dir %s: %s", parent, exc)

    # Remove a stale socket file from a previous run so bind() can succeed.
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _serve_client(core, reader, writer)

    server = await asyncio.start_unix_server(handle, path)
    os.chmod(path, SOCKET_MODE)
    log.info("socket pipe listening at %s", path)
    async with server:
        await server.serve_forever()
