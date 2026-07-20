"""Daemon entry point: ``python -m neewerd [config.toml]`` (or the ``neewerd`` script).

Boot order:

1. Load the TOML config (or run on defaults if none is found).
2. Start :class:`~neewer.fleet.Fleet` — it begins discovering and holding tubes.
3. Start every enabled I/O module as its own task.
4. Run until interrupted, then disconnect cleanly.
"""
from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import signal
import sys
import tomllib

from neewer import devices
from neewer.fleet import DEFAULT_PREFIXES, Fleet

from . import presets as presets_mod


def load_config(path: str | None) -> dict:
    """Load a TOML config file, or return an empty config if ``path`` is falsy."""
    if not path:
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


#: Recognised top-level config keys. An unknown one is almost always a typo whose
#: setting then silently does nothing, so we warn (not abort — forward-compat).
_KNOWN_TOP = {"core", "modules", "presets", "log_level"}
#: Recognised ``[core]`` keys (see :func:`build_core`).
_KNOWN_CORE = {"prefixes", "positions", "rescan_interval", "devices", "liveness_interval"}
#: I/O modules the daemon ships; a ``[modules.<name>]`` under any other name won't
#: import (:func:`start_modules` would log an ImportError), so flag it up front.
_KNOWN_MODULES = {"socket", "http", "mqtt", "osc", "sacn", "artnet", "artnet_bridge"}


def validate_config(cfg: dict) -> list[str]:
    """Return human-readable warnings for unrecognised config keys.

    TOML with a mistyped key (``[modules.htpp]``, ``rescan_intervals``) otherwise
    loads fine and silently drops the setting. This surfaces those as warnings —
    best-effort, non-fatal, so a newer key from a future version doesn't abort an
    older daemon.
    """
    warnings: list[str] = []
    for key in cfg:
        if key not in _KNOWN_TOP:
            warnings.append(f"unknown top-level key {key!r} (known: {sorted(_KNOWN_TOP)})")
    core_cfg = cfg.get("core", {})
    if isinstance(core_cfg, dict):
        for key in core_cfg:
            if key not in _KNOWN_CORE:
                warnings.append(f"unknown [core] key {key!r} (known: {sorted(_KNOWN_CORE)})")
    modules = cfg.get("modules", {})
    if isinstance(modules, dict):
        for name in modules:
            if name not in _KNOWN_MODULES:
                warnings.append(
                    f"unknown module [modules.{name}] (known: {sorted(_KNOWN_MODULES)})")
    return warnings


def resolve_config_path(argv: list[str]) -> str | None:
    """Find the config file by precedence.

    CLI argument > ``$NEEWERD_CONFIG`` > ``./neewerd.toml`` > ``/etc/neewerd/neewerd.toml``.
    Returns ``None`` if none exist (the daemon then runs on built-in defaults).
    """
    if len(argv) > 1:
        return argv[1]
    if os.environ.get("NEEWERD_CONFIG"):
        return os.environ["NEEWERD_CONFIG"]
    for candidate in ("neewerd.toml", "/etc/neewerd/neewerd.toml"):
        if os.path.exists(candidate):
            return candidate
    return None


def build_core(cfg: dict) -> Fleet:
    """Construct the core from the ``[core]`` config section (all keys optional).

    Aliases/groups come from the shared ``~/.config/neewer/devices.toml`` (the same
    file the root scripts read); ``[core].devices`` may point elsewhere. The
    daemon's ``[core.positions]`` still wins over the book's positions per-MAC.
    """
    core_cfg = cfg.get("core", {})
    core = Fleet(
        prefixes=tuple(core_cfg.get("prefixes", DEFAULT_PREFIXES)),
        positions={k.upper(): v for k, v in core_cfg.get("positions", {}).items()},
        rescan_interval=float(core_cfg.get("rescan_interval", 20.0)),
        book=devices.load(core_cfg.get("devices")),
        # Half-open-link liveness-probe staleness threshold, seconds; 0 disables (#47).
        liveness_interval=float(core_cfg.get("liveness_interval", 30.0)),
    )
    # Top-level [presets] (name -> list of command lines) is daemon *policy*, not a
    # library concept: register it as the `preset` verb via the library's generic
    # verb hook, so `dispatch("preset x")` works from every transport.
    core.register_verb("preset", presets_mod.PresetRunner(cfg.get("presets", {})))
    return core


def start_modules(core: Fleet, cfg: dict, log: logging.Logger) -> list[asyncio.Task]:
    """Import and start every enabled I/O module; return their tasks.

    A module that fails to import or start is logged and skipped rather than
    taking the whole daemon down — e.g. ``mqtt`` without ``aiomqtt`` installed.
    """
    tasks: list[asyncio.Task] = []
    for name, module_cfg in cfg.get("modules", {}).items():
        if not module_cfg.get("enabled", False):
            continue
        try:
            module = importlib.import_module(f".modules.{name}", package="neewerd")
            tasks.append(asyncio.create_task(module.run(core, module_cfg), name=f"mod:{name}"))
            log.info("started module %s", name)
        except Exception as exc:
            log.error("failed to start module %s: %s", name, exc)
    return tasks


#: Per-phase grace for teardown. A module (e.g. an mqtt client mid-disconnect) or a
#: hung BLE disconnect gets this long to finish before we abandon it and exit anyway.
SHUTDOWN_TIMEOUT = 5.0


def _install_signal_handlers(loop, stop: asyncio.Event) -> None:
    """Wire SIGINT + SIGTERM to set the stop event (no-op where unsupported).

    Handling SIGTERM too means ``systemctl stop`` / a container ``docker stop``
    shut the daemon down cleanly (which frees the BLE links) instead of timing out
    into a SIGKILL that strands them.
    """
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, stop.set)


def toggle_debug(configured_level: int, log: logging.Logger) -> None:
    """SIGUSR1: flip the root logger between DEBUG and the configured level (#45).

    Lets an operator watch the notify/GATT stream live and dial back afterwards
    without a restart — a restart drops every BLE link and interrupts live output,
    which is exactly what you don't want while diagnosing.
    """
    root = logging.getLogger()
    new = configured_level if root.level == logging.DEBUG else logging.DEBUG
    root.setLevel(new)
    log.warning("log level -> %s (SIGUSR1)", logging.getLevelName(new))


def reset_log_level(configured_level: int, log: logging.Logger) -> None:
    """SIGUSR2: restore the root logger to the configured level (#45)."""
    logging.getLogger().setLevel(configured_level)
    log.warning("log level -> %s (SIGUSR2 reset)", logging.getLevelName(configured_level))


#: ``[core]`` keys SIGHUP deliberately does NOT apply: prefixes/devices change the
#: roster (discovery restart), rescan/liveness intervals are captured by running loops.
_RELOAD_IGNORED_CORE = ("prefixes", "devices", "rescan_interval", "liveness_interval")


def reload_config(core, config_path: str | None, log: logging.Logger) -> None:
    """SIGHUP: re-read the config and hot-apply the safe subset (#45).

    Applied in place, preserving BLE links and running modules:

    - ``[presets]`` — swapped into the live :class:`~neewerd.presets.PresetRunner`.
    - ``[core.positions]`` — merged into ``core.positions`` and stamped onto
      already-discovered tubes (positions are the common live tweak when a
      rotated MAC lands a tube on the wrong slot).

    Everything else is **logged and ignored** — module knobs are read once at
    module startup, and roster keys need a discovery restart — so the operator
    always knows what a HUP did and did not change.
    """
    if not config_path:
        log.warning("SIGHUP: no config file to reload (running on defaults)")
        return
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        log.error("SIGHUP: reload of %s failed, keeping running config: %s", config_path, exc)
        return
    for warning in validate_config(cfg):
        log.warning("config: %s", warning)

    runner = core.verbs.get("preset")
    if runner is not None and hasattr(runner, "presets"):
        runner.presets = {str(k): list(v) for k, v in cfg.get("presets", {}).items()}
        log.info("SIGHUP: presets reloaded (%d defined)", len(runner.presets))

    positions = {k.upper(): v for k, v in cfg.get("core", {}).get("positions", {}).items()}
    if positions:
        core.positions.update(positions)
        for mac, pos in positions.items():
            if mac in core.tubes:
                core.tubes[mac].position = pos
        log.info("SIGHUP: positions merged (%d entries)", len(positions))

    ignored = [k for k in _RELOAD_IGNORED_CORE if k in cfg.get("core", {})]
    if cfg.get("modules"):
        ignored.append("[modules.*] (read at module start; restart to apply)")
    if ignored:
        log.info("SIGHUP: not hot-reloadable, ignored: %s", ", ".join(ignored))


def _install_runtime_signal_handlers(loop, core, config_path: str | None,
                                     configured_level: int, log: logging.Logger) -> None:
    """Wire the tune-without-restart signals (#45): SIGUSR1/2 log level, SIGHUP reload."""
    # On Windows, SIGUSR1/SIGUSR2/SIGHUP don't exist, so skip signal setup entirely
    handlers = []
    with contextlib.suppress(AttributeError):
        handlers.append((signal.SIGUSR1, lambda: toggle_debug(configured_level, log)))
    with contextlib.suppress(AttributeError):
        handlers.append((signal.SIGUSR2, lambda: reset_log_level(configured_level, log)))
    with contextlib.suppress(AttributeError):
        handlers.append((signal.SIGHUP, lambda: reload_config(core, config_path, log)))

    for sig, handler in handlers:
        with contextlib.suppress(NotImplementedError, ValueError):
            loop.add_signal_handler(sig, handler)


async def shutdown(tasks, core, log, timeout: float = SHUTDOWN_TIMEOUT) -> None:
    """Cancel the module tasks and stop the core, each bounded by ``timeout``.

    Awaiting the cancelled module tasks (rather than just cancelling and dropping
    them) is what lets an ``aiomqtt`` client run its ``__aexit__`` and stop its
    background thread before the loop closes — otherwise it errors with
    "Event loop is closed" and can wedge shutdown. The core stop is likewise
    time-bounded so a hung BLE disconnect can't block process exit forever.
    """
    for task in tasks:
        task.cancel()
    if tasks:
        _done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            log.warning("%d module task(s) did not stop within %.0fs; abandoning",
                        len(pending), timeout)
    try:
        await asyncio.wait_for(core.stop(), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning("core.stop() timed out after %.0fs; forcing exit", timeout)
    except Exception as exc:
        log.warning("core.stop() error: %s", exc)


async def main(cfg: dict, config_path: str | None = None) -> None:
    """Configure logging, start the core and modules, and run until a signal."""
    configured_level = getattr(logging, cfg.get("log_level", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=configured_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("neewerd")

    for warning in validate_config(cfg):
        log.warning("config: %s", warning)

    core = build_core(cfg)
    await core.start()

    tasks = start_modules(core, cfg, log)
    if not tasks:
        log.warning("no modules enabled — core is holding tubes but nothing can talk to it")

    # Run until SIGINT/SIGTERM, or until every module task has exited on its own.
    stop = asyncio.Event()
    _install_signal_handlers(asyncio.get_running_loop(), stop)
    # Tune-without-restart signals (#45): SIGUSR1 debug toggle, SIGUSR2 reset,
    # SIGHUP config reload — none of them touch the held BLE links.
    _install_runtime_signal_handlers(asyncio.get_running_loop(), core,
                                     config_path, configured_level, log)
    for task in tasks:
        task.add_done_callback(lambda _t: stop.set())

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass                                    # fallback if signal handlers are unavailable
    finally:
        log.info("shutting down")
        await shutdown(tasks, core, log)


def cli() -> None:
    """Console-script entry point (see ``[project.scripts]`` in pyproject)."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="neewerd",
        description="Neewer TL-series RGB tube light control daemon",
        epilog="See https://github.com/verygeeky/neewerd for documentation",
    )
    parser.add_argument(
        "config",
        nargs="?",
        help=(
            "path to config file (default: neewerd.toml, "
            "/etc/neewerd/neewerd.toml, or $NEEWERD_CONFIG)"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="neewerd 0.1.1",
    )
    args = parser.parse_args()

    # Use the provided config path, or fall back to resolve_config_path logic
    path = args.config if args.config else resolve_config_path(sys.argv)
    try:
        asyncio.run(main(load_config(path), config_path=path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
