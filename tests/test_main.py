"""Tests for :mod:`neewerd.__main__` config loading and core construction.

Only the pure, side-effect-free helpers are exercised: ``load_config``,
``resolve_config_path`` (precedence) and ``build_core``. The async ``main``
boot path needs a real radio and is out of scope.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from neewer.fleet import DEFAULT_PREFIXES
from neewer.fleet import Fleet as NeewerCore

from neewerd import __main__ as entry


def _run(coro):
    return asyncio.run(coro)

# --- validate_config ------------------------------------------------------

def test_validate_config_accepts_known_keys():
    cfg = {
        "log_level": "INFO",
        "core": {"prefixes": ["NW-"], "positions": {}, "rescan_interval": 20,
                 "devices": None},
        "modules": {"http": {"enabled": True}, "mqtt": {"enabled": False}},
        "presets": {"warm": ["all cct 90 48"]},
    }
    assert entry.validate_config(cfg) == []


def test_validate_config_flags_unknown_keys():
    cfg = {
        "logg_level": "INFO",                     # typo'd top-level key
        "core": {"rescan_intervals": 20},         # typo'd [core] key
        "modules": {"htpp": {"enabled": True}},   # typo'd module name
    }
    warnings = entry.validate_config(cfg)
    joined = " ".join(warnings)
    assert "logg_level" in joined
    assert "rescan_intervals" in joined
    assert "htpp" in joined
    assert len(warnings) == 3


# --- load_config ----------------------------------------------------------

def test_load_config_none_returns_empty():
    assert entry.load_config(None) == {}


def test_load_config_reads_toml(tmp_path):
    cfg_file = tmp_path / "neewerd.toml"
    cfg_file.write_text('log_level = "DEBUG"\n[core]\nrescan_interval = 5.0\n')
    cfg = entry.load_config(str(cfg_file))
    assert cfg["log_level"] == "DEBUG"
    assert cfg["core"]["rescan_interval"] == 5.0


# --- resolve_config_path precedence --------------------------------------

def test_resolve_path_cli_arg_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("NEEWERD_CONFIG", "/env/path.toml")
    # argv[1] takes precedence over the env var and any file on disk.
    assert entry.resolve_config_path(["neewerd", "/cli/path.toml"]) == "/cli/path.toml"


def test_resolve_path_env_var_second(monkeypatch, tmp_path):
    monkeypatch.setenv("NEEWERD_CONFIG", "/env/path.toml")
    assert entry.resolve_config_path(["neewerd"]) == "/env/path.toml"


def test_resolve_path_local_file_third(monkeypatch, tmp_path):
    monkeypatch.delenv("NEEWERD_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "neewerd.toml").write_text("")
    assert entry.resolve_config_path(["neewerd"]) == "neewerd.toml"


def test_resolve_path_none_when_nothing_present(monkeypatch, tmp_path):
    monkeypatch.delenv("NEEWERD_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # empty dir, no config file
    # /etc/neewerd/neewerd.toml is assumed absent in the test environment.
    assert entry.resolve_config_path(["neewerd"]) is None


# --- build_core -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_device_book(monkeypatch, tmp_path):
    """Point the shared device book at a nonexistent file so build_core tests
    don't pick up (or depend on) a real ~/.config/neewer/devices.toml."""
    monkeypatch.setenv("NEEWER_DEVICES", (tmp_path / "none.toml").as_posix())


def test_build_core_defaults():
    core = entry.build_core({})
    assert isinstance(core, NeewerCore)
    assert core.prefixes == tuple(DEFAULT_PREFIXES)
    assert core.rescan_interval == 20.0
    assert core.positions == {}


def test_build_core_from_config():
    cfg = {
        "core": {
            "prefixes": ["NW-", "SL"],
            "positions": {"aa:bb:cc:dd:ee:ff": 1},
            "rescan_interval": 7,
        }
    }
    core = entry.build_core(cfg)
    assert core.prefixes == ("NW-", "SL")
    # Positions are upper-cased for case-insensitive MAC lookup.
    assert core.positions == {"AA:BB:CC:DD:EE:FF": 1}
    assert core.rescan_interval == 7.0
    assert isinstance(core.rescan_interval, float)


def test_build_core_loads_device_book(monkeypatch, tmp_path):
    # A [core].devices path is honoured, and its aliases/groups reach resolve().
    dev = tmp_path / "devices.toml"
    dev.write_text(
        '[aliases]\nkey = "AA:BB:CC:DD:EE:FF"\n'
        '[groups]\nmine = ["key"]\n'
        '[positions]\nkey = 5\n'
    )
    core = entry.build_core({"core": {"devices": dev.as_posix()}})
    assert core.book.resolve_one("key") == "AA:BB:CC:DD:EE:FF"
    assert core.book.expand("mine") == ["AA:BB:CC:DD:EE:FF"]
    # book positions merged into the core's position map
    assert core.positions["AA:BB:CC:DD:EE:FF"] == 5


def test_build_core_registers_presets_verb():
    cfg = {"presets": {"recording": ["all cct 90 48 50"],
                       "off": ["all power off"]}}
    core = entry.build_core(cfg)
    # Presets are now a daemon verb registered on the core, not a Fleet attribute.
    runner = core.verbs["preset"]
    assert runner.presets["recording"] == ["all cct 90 48 50"]
    assert runner.presets["off"] == ["all power off"]


def test_build_core_no_presets_is_empty():
    assert entry.build_core({}).verbs["preset"].presets == {}


def test_build_core_toml_positions_override_book(tmp_path):
    dev = tmp_path / "devices.toml"
    dev.write_text('[positions]\n"AA:BB:CC:DD:EE:FF" = 5\n')
    cfg = {"core": {"devices": dev.as_posix(),
                    "positions": {"aa:bb:cc:dd:ee:ff": 9}}}
    core = entry.build_core(cfg)
    # explicit [core.positions] wins over the book's value for the same MAC
    assert core.positions["AA:BB:CC:DD:EE:FF"] == 9


# --- graceful shutdown (issue #31) ----------------------------------------

class _FakeCore:
    def __init__(self, stop_delay=0.0):
        self.stopped = False
        self._stop_delay = stop_delay

    async def stop(self):
        await asyncio.sleep(self._stop_delay)
        self.stopped = True


def test_shutdown_cancels_tasks_and_stops_core():
    async def body():
        async def forever():
            await asyncio.Event().wait()        # a module task that never returns
        tasks = [asyncio.ensure_future(forever()) for _ in range(2)]
        await asyncio.sleep(0)                   # let them start
        core = _FakeCore()
        await entry.shutdown(tasks, core, logging.getLogger("t"), timeout=1.0)
        assert all(t.cancelled() or t.done() for t in tasks)
        assert core.stopped is True
    _run(body())


def test_shutdown_bounds_a_hung_task():
    async def body():
        async def wedged():
            # swallow the first cancel and keep running -> must be abandoned, not hang
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.Event().wait()
        task = asyncio.ensure_future(wedged())
        await asyncio.sleep(0)
        core = _FakeCore()
        # a short timeout: shutdown returns despite the wedged task
        await asyncio.wait_for(
            entry.shutdown([task], core, logging.getLogger("t"), timeout=0.2), timeout=2.0)
        assert core.stopped is True
        task.cancel()
    _run(body())


def test_shutdown_bounds_a_hung_core_stop():
    async def body():
        core = _FakeCore(stop_delay=10.0)        # core.stop() that hangs
        # must return within the timeout, not block on the 10s stop
        await asyncio.wait_for(
            entry.shutdown([], core, logging.getLogger("t"), timeout=0.2), timeout=2.0)
        assert core.stopped is False             # timed out, but we didn't hang
    _run(body())


# --- runtime signals (#45): log-level toggle + SIGHUP config reload ----------

def _quiet_logger():
    log = logging.getLogger("neewerd-test-signals")
    log.setLevel(logging.CRITICAL)                     # keep test output clean
    return log


def test_toggle_debug_flips_and_restores_root_level():
    root = logging.getLogger()
    original = root.level
    try:
        root.setLevel(logging.INFO)
        entry.toggle_debug(logging.INFO, _quiet_logger())
        assert root.level == logging.DEBUG
        entry.toggle_debug(logging.INFO, _quiet_logger())   # toggle back
        assert root.level == logging.INFO
    finally:
        root.setLevel(original)


def test_reset_log_level_restores_configured():
    root = logging.getLogger()
    original = root.level
    try:
        root.setLevel(logging.DEBUG)
        entry.reset_log_level(logging.WARNING, _quiet_logger())
        assert root.level == logging.WARNING
    finally:
        root.setLevel(original)


def test_reload_config_swaps_presets_and_merges_positions(tmp_path):
    cfg_file = tmp_path / "neewerd.toml"
    cfg_file.write_text(
        '[presets]\nwarm = ["all cct 90 48"]\n'
        '[core.positions]\n"aa:bb:cc:dd:ee:01" = 4\n'
    )
    core = entry.build_core({"presets": {"old": ["all power off"]}})
    # A tube discovered before the reload gets its position restamped in place.
    from neewer.fleet import Tube
    core.tubes["AA:BB:CC:DD:EE:01"] = Tube("AA:BB:CC:DD:EE:01", name="NW-test", position=None)

    entry.reload_config(core, cfg_file.as_posix(), _quiet_logger())

    runner = core.verbs["preset"]
    assert runner.presets == {"warm": ["all cct 90 48"]}   # old table fully replaced
    assert core.positions["AA:BB:CC:DD:EE:01"] == 4
    assert core.tubes["AA:BB:CC:DD:EE:01"].position == 4


def test_reload_config_bad_file_keeps_running_config(tmp_path):
    bad = tmp_path / "broken.toml"
    bad.write_text("this is not [valid toml")
    core = entry.build_core({"presets": {"keep": ["all power on"]}})
    entry.reload_config(core, bad.as_posix(), _quiet_logger())
    assert core.verbs["preset"].presets == {"keep": ["all power on"]}


def test_reload_config_no_path_is_a_noop():
    core = entry.build_core({})
    entry.reload_config(core, None, _quiet_logger())    # must not raise


def test_build_core_passes_liveness_interval():
    core = entry.build_core({"core": {"liveness_interval": 12}})
    assert core.liveness_interval == 12.0
    # Default when unset: the library's 30 s.
    assert entry.build_core({}).liveness_interval == 30.0
