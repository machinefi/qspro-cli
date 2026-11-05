import sys

import httpx
import pytest


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("QSP_CONFIG_DIR", str(tmp_path / ".config/quicksilverpro"))
    monkeypatch.setenv("QSP_MACHINES_KEY", "qsm_test_only")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("QSP_MACHINES_URL", raising=False)

    # Any unmocked HTTP request fails rather than reaching a real service.
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must not use the network")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)


@pytest.fixture
def mock_api(monkeypatch):
    original = httpx.Client
    calls = []

    def install(handler):
        def wrapped(request):
            calls.append(request)
            return handler(request)

        transport = httpx.MockTransport(wrapped)

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", factory)
        return calls

    return install
