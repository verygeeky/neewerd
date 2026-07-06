"""Where the command socket lives — resolved identically by the daemon and the CLI.

Precedence (first hit wins):

1. ``$NEEWERD_SOCKET`` — explicit override.
2. ``$XDG_RUNTIME_DIR/neewerd.sock`` — the right place for a **user** service
   (systemd sets this to ``/run/user/<uid>``; it's per-user and cleaned on logout).
3. ``/run/neewerd/neewerd.sock`` — for a **system** service, when that directory
   exists and is writable (create it with systemd ``RuntimeDirectory=neewerd``).
4. ``/tmp/neewerd.sock`` — last-resort fallback.

Keeping this in one place means ``neewerctl`` always looks where ``neewerd`` listens.
"""
from __future__ import annotations

import os

ENV_VAR = "NEEWERD_SOCKET"


def default_socket_path() -> str:
    """Return the socket path per the precedence documented above."""
    override = os.environ.get(ENV_VAR)
    if override:
        return override

    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isdir(xdg):
        return os.path.join(xdg, "neewerd.sock")

    system_run = "/run/neewerd"
    if os.path.isdir(system_run) and os.access(system_run, os.W_OK):
        return os.path.join(system_run, "neewerd.sock")

    return "/tmp/neewerd.sock"
