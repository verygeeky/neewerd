"""osc2neewer — drive tubes from OSC (TouchOSC, QLab, Max/MSP, Resolume, ...).

The OSC address and arguments are mapped to a command line by
:func:`neewer.grammar.osc_to_command`::

    /neewer/all/hsi      240 100 80
    /neewer/t1/cct       80 56
    /neewer/all/flow     palette          (effect mode + opts as string args)
    /neewer/all/power    on

Requires the optional ``python-osc`` dependency (``pip install '.[osc]'``).

Threading note: ``python-osc`` invokes handlers on its own thread, so we hop back
onto the asyncio loop with ``run_coroutine_threadsafe`` before touching ``core``.
"""
from __future__ import annotations

import asyncio
import logging

from neewer.grammar import osc_to_command
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import AsyncIOOSCUDPServer

log = logging.getLogger("neewerd.osc")

IDLE_TICK = 3600.0            # the serve loop just parks; the server runs in the background


#: Loopback addresses that don't warrant the "network-exposed" warning.
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


async def run(core, cfg) -> None:
    """Listen for OSC messages and dispatch each as a command line."""
    # Loopback by default: OSC has no auth, so a network bind is an explicit choice.
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 9000))
    if host not in _LOOPBACK:
        log.warning("osc bound to %s (non-loopback) with no auth — anyone on the "
                    "network can control the lights. Bind 127.0.0.1 unless intended.", host)
    loop = asyncio.get_running_loop()

    def handler(address, *args) -> None:
        # Called on python-osc's thread; marshal back onto the asyncio loop.
        line = osc_to_command(address, args)
        asyncio.run_coroutine_threadsafe(_dispatch(core, line), loop)

    dispatcher = Dispatcher()
    dispatcher.set_default_handler(handler)

    server = AsyncIOOSCUDPServer((host, port), dispatcher, loop)
    transport, _ = await server.create_serve_endpoint()
    log.info("osc listening on %s:%s", host, port)
    try:
        while True:
            await asyncio.sleep(IDLE_TICK)
    finally:
        transport.close()


async def _dispatch(core, line: str) -> None:
    """Run one command line on the loop and log the outcome."""
    try:
        reply = await core.dispatch(line)
        log.info("osc %r -> %s", line, reply)
    except Exception as exc:
        log.warning("osc %r error: %s", line, exc)
