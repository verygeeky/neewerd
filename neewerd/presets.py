"""Config-defined presets — a daemon policy layered onto the library grammar.

A preset is a named, ordered list of command lines from the daemon's ``[presets]``
config. Running one re-dispatches each line through the normal grammar, so a preset
can span multiple targets/actions and even start an effect.

This lives in the daemon, **not** the ``neewer`` library, because presets are
*policy* — what a given deployment wants a name to mean — not protocol. The library
exposes a generic verb hook (:meth:`neewer.fleet.Fleet.register_verb`); the daemon
registers this runner under the ``preset`` verb (see :func:`neewerd.__main__.build_core`),
so ``core.dispatch("preset warm")`` works from every transport without the library
owning the preset concept.
"""
from __future__ import annotations

from neewer.errors import UnknownPreset


class PresetRunner:
    """Holds the preset table and runs a preset by name, guarding against cycles.

    Registered on the fleet as the ``preset`` verb; being callable *is* the verb
    handler. Exposing :attr:`presets` lets the HTTP ``/api/v1/presets`` discovery
    route list them without the core library carrying a preset attribute.
    """

    def __init__(self, presets=None):
        #: name -> ordered command lines. Copied/normalised at construction so a
        #: later config edit can't mutate a live runner.
        self.presets: dict[str, list[str]] = {
            str(k): list(v) for k, v in (presets or {}).items()
        }
        #: Guards recursion (a preset whose line runs another preset) so a cycle
        #: breaks instead of looping forever.
        self._running: set[str] = set()

    async def __call__(self, fleet, args) -> str:
        """Verb handler for ``preset <name>`` — run the named preset's lines in order.

        Receives the raw trailing words from the grammar. Each line is re-dispatched
        through ``fleet.dispatch`` (so it reuses the whole grammar). An unknown name
        raises :class:`~neewer.errors.UnknownPreset`; a preset that (transitively)
        invokes itself is skipped by :attr:`_running` rather than recursing forever.
        """
        name = args[0] if args else ""
        lines = self.presets.get(name)
        if lines is None:
            raise UnknownPreset(name)
        if name in self._running:
            return f"ok preset {name!r} skipped (already running)"
        self._running.add(name)
        try:
            results = [await fleet.dispatch(line) for line in lines]
        finally:
            self._running.discard(name)
        return f"ok preset {name!r}: " + " | ".join(results)
