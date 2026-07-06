"""Dry tests for :mod:`neewerd.modules.osc` — the OSC dispatch helper.

The address->command-line mapping (``osc_to_command``) is covered in
``test_commands.py``; here we cover the module's ``_dispatch`` (run one line, log
the outcome, never raise into python-osc's thread). python-osc itself is stubbed
so the module imports without the optional dependency.
"""
from __future__ import annotations

import asyncio
import sys
import types

# stub pythonosc so `import ...osc_server` in the module resolves without the extra.
# Only when the real package is absent (CI) — otherwise a live osc run must get the
# real pythonosc, not this stub.
try:
    import pythonosc.osc_server  # noqa: F401
except ImportError:
    for name in ("pythonosc", "pythonosc.dispatcher", "pythonosc.osc_server"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["pythonosc.dispatcher"].Dispatcher = type("Dispatcher", (), {})
    sys.modules["pythonosc.osc_server"].AsyncIOOSCUDPServer = type(
        "AsyncIOOSCUDPServer", (), {})

from neewerd.modules import osc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeCore:
    def __init__(self, reply="ok"):
        self.reply = reply
        self.dispatched: list[str] = []

    async def dispatch(self, line):
        self.dispatched.append(line)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_dispatch_runs_the_line():
    core = FakeCore()
    run(osc._dispatch(core, "all hsi 240 100 80"))
    assert core.dispatched == ["all hsi 240 100 80"]


def test_dispatch_swallows_errors():
    core = FakeCore(reply=ValueError("bad"))
    # must not raise — python-osc's thread would otherwise die
    run(osc._dispatch(core, "all hsi x"))
    assert core.dispatched == ["all hsi x"]
