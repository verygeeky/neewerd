"""Pluggable I/O frontends. Each module exposes `async def run(core, cfg)`.

A module translates its wire protocol into command lines and calls
`core.dispatch(line)`; that's the entire contract. Enable modules in the config.
"""
