"""Shared test fixtures and import-time stubs for the neewerd suite.

The application modules import ``bleak`` (and the optional I/O modules import
``aiomqtt`` / ``pythonosc``) at module top, but none of those packages are
installed in the test environment — and we never want tests to touch a real
radio anyway. So before anything else, we register lightweight stub modules in
``sys.modules`` so ``import bleak`` succeeds with no-op classes. Every test that
exercises real Bluetooth does so against an explicit fake, never these stubs.

We also put the repo root on ``sys.path`` so the standalone root scripts
(``ctl.py`` / ``pixel.py`` / ``flow.py`` / ``watch.py``) can be imported by name.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _install_bleak_stub() -> None:
    """Register a minimal fake ``bleak`` so imports resolve without the package.

    The stubs are deliberately inert: calling them is a test bug, so they raise
    if anyone tries to actually scan/connect. Tests that need BLE behaviour
    inject their own fakes (see ``fake_tube`` etc.).
    """
    if "bleak" in sys.modules:
        return
    bleak = types.ModuleType("bleak")

    class BleakClient:  # noqa: D401 - stub
        """Inert stand-in; real client behaviour is faked per-test."""

        def __init__(self, *args, **kwargs):
            self._args = args
            self._kwargs = kwargs

        async def connect(self, *a, **k):
            raise RuntimeError("stub BleakClient.connect must never run in tests")

        async def disconnect(self, *a, **k):
            return None

        async def write_gatt_char(self, *a, **k):
            raise RuntimeError("stub BleakClient.write_gatt_char must never run")

    class BleakScanner:  # noqa: D401 - stub
        """Inert stand-in for the scanner."""

        def __init__(self, *args, **kwargs):
            pass

        @staticmethod
        async def discover(*a, **k):
            raise RuntimeError("stub BleakScanner.discover must never run in tests")

        @staticmethod
        async def find_device_by_address(*a, **k):
            raise RuntimeError("stub find_device_by_address must never run")

        async def start(self):
            return None

        async def stop(self):
            return None

    bleak.BleakClient = BleakClient
    bleak.BleakScanner = BleakScanner
    sys.modules["bleak"] = bleak


_install_bleak_stub()

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# --- live-test gating -----------------------------------------------------
# Tests marked @pytest.mark.live talk to a *running* daemon and real hardware.
# They are collected but skipped unless NEEWER_LIVE=1, so the default `pytest`
# run stays hardware-free. Run them with:  NEEWER_LIVE=1 pytest -m live
def pytest_collection_modifyitems(config, items):
    if os.environ.get("NEEWER_LIVE") == "1":
        return
    skip_live = pytest.mark.skip(
        reason="live test — start the daemon and set NEEWER_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
