import os
import signal
import sys
from contextlib import contextmanager

import pytest

pytest.importorskip("websockets")

from quicksilverpro.machines import MachineError
from quicksilverpro.terminal import bridge

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX terminal bridge")


@pytest.mark.parametrize("failure", [False, True])
def test_terminal_restored_on_disconnect_and_failure(monkeypatch, failure):
    import pty
    import termios

    import websockets.sync.client

    master, slave = pty.openpty()
    saved = termios.tcgetattr(slave)
    original_signals = {
        s: signal.getsignal(s) for s in (signal.SIGWINCH, signal.SIGTERM, signal.SIGHUP)
    }

    class Input:
        def fileno(self):
            return slave

    monkeypatch.setattr(sys, "stdin", Input())
    monkeypatch.setattr("quicksilverpro.terminal.select.select", lambda *args: ([slave], [], []))
    monkeypatch.setattr("quicksilverpro.terminal.os.read", lambda *args: b"\x1d")
    sent = []

    class Socket:
        def send(self, data):
            sent.append(data)
            if failure:
                raise OSError("connection lost")

    @contextmanager
    def connect(*args, **kwargs):
        assert "authorization" not in str(kwargs).lower()
        yield Socket()

    monkeypatch.setattr(websockets.sync.client, "connect", connect)
    try:
        if failure:
            with pytest.raises(MachineError):
                bridge("wss://files.example/capability")
        else:
            bridge("wss://files.example/capability")
        restored = termios.tcgetattr(slave)
        # Some kernels set PENDIN while restoring cooked mode (pending-input bookkeeping).
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        saved[3] &= ~getattr(termios, "PENDIN", 0)
        assert restored == saved
        assert {s: signal.getsignal(s) for s in original_signals} == original_signals
        assert '"resize"' in sent[0]
    finally:
        os.close(master)
        os.close(slave)
