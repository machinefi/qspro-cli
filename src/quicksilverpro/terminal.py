"""POSIX TTY bridge. Ctrl-] disconnects; raw Ctrl-C is forwarded to the guest."""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import sys

from .machines import MachineError


def bridge(url):
    if os.name != "posix":
        raise MachineError(
            "unsupported_terminal", "Interactive shell currently requires a POSIX terminal.", 2
        )
    import termios
    import tty

    try:
        from websockets.exceptions import ConnectionClosedOK
        from websockets.sync.client import connect
    except ImportError:
        raise MachineError("missing_extra", "Install quicksilverpro[shell] to use shell.", 2)
    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    previous = {}
    resized = True
    stopped = False

    def resize(*args):
        nonlocal resized
        resized = True

    def stop(*args):
        nonlocal stopped
        stopped = True

    try:
        previous[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, resize)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, stop)
        # Never forward Machines authorization to the capability endpoint.
        with connect(url, open_timeout=10, close_timeout=2, max_size=65536, proxy=None) as ws:
            tty.setraw(fd)
            while not stopped:
                if resized:
                    size = shutil.get_terminal_size()
                    ws.send(
                        json.dumps({"type": "resize", "cols": size.columns, "rows": size.lines})
                    )
                    resized = False
                if select.select([fd], [], [], 0.02)[0]:
                    data = os.read(fd, 4096)
                    if not data or b"\x1d" in data:
                        break
                    ws.send(data)
                try:
                    message = ws.recv(timeout=0.02)
                except TimeoutError:
                    continue
                except ConnectionClosedOK:
                    break
                data = message.encode() if isinstance(message, str) else message
                view = memoryview(data)
                while view:
                    view = view[os.write(sys.stdout.fileno(), view) :]
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise MachineError("terminal_error", "Terminal connection failed or disconnected.")
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, original)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    if stopped:
        raise MachineError("interrupted", "Terminal interrupted.", 130)
