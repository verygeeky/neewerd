"""neewerctl — a thin client for the neewerd command socket.

It opens the daemon's Unix socket, sends one command line, and prints the reply —
so you don't have to hand-roll ``nc -U`` / socket one-liners. (For driving the
hardware directly without a running daemon, use the ``neewer`` wrapper instead.)

    neewerctl all hsi 240 100 80
    neewerctl all power off
    neewerctl query                 # ask every tube for battery/state/version
    neewerctl state --json          # pretty-print the cached state snapshot
    echo 'all power on' | neewerctl  # pipe mode: one command per stdin line

The socket path is resolved exactly like the daemon resolves it (see
:mod:`neewerd.socketpath`); override with ``--socket`` or ``$NEEWERD_SOCKET``.
Depends only on the standard library.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys

from .socketpath import default_socket_path

REPLY_TIMEOUT = 5.0


def send_line(path: str, line: str, timeout: float = REPLY_TIMEOUT) -> str:
    """Send one command line to the daemon socket and return its single-line reply."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(path)
        sock.sendall((line.rstrip("\n") + "\n").encode())
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    return data.decode(errors="replace").strip()


def _format(reply: str, as_json: bool) -> str:
    """Optionally pretty-print a JSON reply (e.g. from ``state``); else pass through."""
    if not as_json:
        return reply
    try:
        return json.dumps(json.loads(reply), indent=2, sort_keys=True)
    except ValueError:
        return reply            # not JSON (an "ok …" / "error …" line) — show as-is


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="neewerctl", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-s", "--socket", default=default_socket_path(),
                        help="daemon socket path (default: auto-resolved)")
    parser.add_argument("--json", action="store_true",
                        help="pretty-print JSON replies (handy for 'state')")
    parser.add_argument("words", nargs="*",
                        help="command to send, e.g. all hsi 240 100 80")
    args = parser.parse_args()

    try:
        if args.words:
            print(_format(send_line(args.socket, " ".join(args.words)), args.json))
        else:
            # No command given: read one command per line from stdin (pipe mode).
            for line in sys.stdin:
                line = line.strip()
                if line:
                    print(_format(send_line(args.socket, line), args.json))
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        sys.exit(f"neewerctl: cannot reach daemon at {args.socket} ({exc}). "
                 f"Is neewerd running?")
    except socket.timeout:
        sys.exit("neewerctl: timed out waiting for a reply")


if __name__ == "__main__":
    main()
