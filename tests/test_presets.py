"""Tests for :mod:`neewerd.presets` — the daemon's config-defined preset runner.

Presets used to live on the library's ``Fleet``; they are now a daemon policy
layered on via the library's generic verb hook. These exercise the runner against
a tiny fake fleet that mimics just the two things it touches: ``dispatch`` (which
routes a registered verb back into the runner, like the real grammar does) and the
``verbs`` registry. No BLE, no real grammar.
"""
from __future__ import annotations

import asyncio

import pytest
from neewer.errors import UnknownPreset

from neewerd.presets import PresetRunner


def run(coro):
    return asyncio.run(coro)


class FakeFleet:
    """Records dispatched lines; routes ``preset ...`` back through a registered verb
    the way :func:`neewer.grammar.dispatch` does, so recursion guards are exercised."""

    def __init__(self):
        self.lines: list[str] = []
        self.verbs: dict = {}

    async def dispatch(self, line: str) -> str:
        self.lines.append(line)
        parts = line.split()
        if parts and parts[0] in self.verbs:            # mirror the grammar's verb hook
            return await self.verbs[parts[0]](self, parts[1:])
        return f"ok {line}"


def _fleet_with(runner: PresetRunner) -> FakeFleet:
    fleet = FakeFleet()
    fleet.verbs["preset"] = runner
    return fleet


def test_preset_runs_each_line_in_order():
    runner = PresetRunner({"scene1": ["t1 power on", "all hsi 240 100 80"]})
    fleet = _fleet_with(runner)
    result = run(runner(fleet, ["scene1"]))
    assert result.startswith("ok preset 'scene1'")
    assert fleet.lines == ["t1 power on", "all hsi 240 100 80"]


def test_preset_unknown_name_raises():
    runner = PresetRunner({"a": ["all power off"]})
    with pytest.raises(UnknownPreset):
        run(runner(_fleet_with(runner), ["ghost"]))


def test_preset_empty_name_raises():
    runner = PresetRunner({"a": ["all power off"]})
    with pytest.raises(UnknownPreset):
        run(runner(_fleet_with(runner), []))


def test_preset_cycle_is_broken():
    # a -> runs preset b -> runs preset a (guard stops the second entry to a)
    runner = PresetRunner({"a": ["preset b", "AA power on"],
                           "b": ["preset a", "AA power off"]})
    fleet = _fleet_with(runner)
    result = run(runner(fleet, ["a"]))
    assert "skipped (already running)" in result
    # both leaf lines still ran once; no infinite recursion
    assert "AA power on" in fleet.lines and "AA power off" in fleet.lines


def test_runner_table_is_copied_not_aliased():
    src = {"a": ["all power off"]}
    runner = PresetRunner(src)
    src["a"].append("mutated")
    assert runner.presets["a"] == ["all power off"]     # construction snapshotted it
