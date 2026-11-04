import hashlib
import inspect
import json

import httpx
import pytest
from click.testing import CliRunner

from quicksilverpro.cli import main
from quicksilverpro.machines import (
    MachineError,
    workspace_path,
)

MID = "abcdef012345"


def run(*args):
    runner = (
        CliRunner(mix_stderr=False)
        if "mix_stderr" in inspect.signature(CliRunner).parameters
        else CliRunner()
    )
    return runner.invoke(main, ["box", *args])


def test_json_auth_separate(monkeypatch):
    monkeypatch.delenv("QSP_MACHINES_KEY")
    monkeypatch.setenv("QSP_API_KEY", "sk_inference")
    result = run("ls", "--json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "auth_required"


@pytest.mark.parametrize(
    "args",
    [
        ("exec", MID, "--argv-json", "null"),
        ("exec", MID),
        ("get", "bad"),
        ("rm", MID),
        ("shell", MID),
    ],
)
def test_usage_json(args):
    result = run(*args, "--json")
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["kind"] == "cli_error"


@pytest.mark.parametrize(
    "code,kind", [(401, 2), (403, 2), (402, 1), (409, 1), (429, 1), (503, 1), (302, 1)]
)
def test_http_failures_no_secrets(mock_api, code, kind):
    calls = mock_api(
        lambda r: httpx.Response(
            code,
            json={"error": {"code": "denied", "message": "qsm_secret"}},
            headers={"Location": "https://other.example"},
        )
    )
    result = run("ls", "--json")
    assert result.exit_code == kind
    assert "qsm_secret" not in result.output
    assert len(calls) == 1


@pytest.mark.parametrize(
    "guest,expected",
    [
        ({"exit_code": 2}, 2),
        ({"exit_code": 0}, 0),
        ({"exit_code": 124}, 124),
        ({"exit_code": None, "timed_out": True}, 124),
        ({"exit_code": None, "signal": "TERM"}, 143),
        ({"exit_code": 0, "truncated": True}, 1),
    ],
)
def test_exec_exits(mock_api, guest, expected):
    mock_api(lambda r: httpx.Response(200, json={"stdout": "ok", "stderr": "warning", **guest}))
    result = run("exec", MID, "--argv-json", '["echo","$(touch no)"]', "--json")
    assert result.exit_code == expected, result.output
    assert json.loads(result.stdout)["kind"] == "guest_exit"


def test_exec_streams_separate(mock_api):
    mock_api(
        lambda r: httpx.Response(200, json={"exit_code": 7, "stdout": "hello", "stderr": "warning"})
    )
    result = run("exec", MID, "--command", "printf hello")
    assert result.exit_code == 7
    assert result.stdout == "hello"
    assert result.stderr == "warning"


def test_exec_unknown_no_retry(mock_api):
    def lost(request):
        raise httpx.ReadError("qsm_secret")

    calls = mock_api(lost)
    result = run("exec", MID, "--argv-json", '["true"]', "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "outcome_unknown"
    assert len(calls) == 1
    assert "qsm_secret" not in result.output


def test_create_lost_response_deduplication(mock_api, tmp_path):
    received = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"prices": {"2": 0.02}, "limits": {"stored": 1}})
        received.append(json.loads(request.content))
        if len(received) == 1:
            raise httpx.ReadError("lost")
        return httpx.Response(201, json={"machine": {"id": MID}})

    mock_api(handler)
    first = run("create", "--name", "work", "--json")
    second = run("create", "--name", "work", "--json")
    assert first.exit_code == 1
    assert second.exit_code == 0
    assert received[0] == received[1]
    journal = tmp_path / ".config/quicksilverpro/create-requests.json"
    assert journal.stat().st_mode & 0o777 == 0o600
    assert journal.parent.stat().st_mode & 0o777 == 0o700
    assert "qsm_" not in journal.read_text()


def test_wait_requires_guest_ready(mock_api, monkeypatch):
    states = iter(
        [{"state": "Running", "guest_ready": False}, {"state": "Running", "guest_ready": True}]
    )
    calls = mock_api(lambda r: httpx.Response(200, json={"machine": next(states)}))
    monkeypatch.setattr("quicksilverpro.machines.time.sleep", lambda n: None)
    assert run("wait", MID, "--json").exit_code == 0
    assert len(calls) == 2


def test_wait_timeout_never_mutates(mock_api, monkeypatch):
    calls = mock_api(lambda r: httpx.Response(200, json={"state": "Running", "guest_ready": False}))
    clock = iter([0, 0, 0, 2])
    monkeypatch.setattr("quicksilverpro.machines.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("quicksilverpro.machines.time.sleep", lambda n: None)
    result = run("wait", MID, "--timeout", "1", "--json")
    assert result.exit_code == 124, result.output
    assert all(r.method == "GET" for r in calls)


@pytest.mark.parametrize(
    "stream,expected",
    [
        (
            'event: stdout\ndata: {"seq":1,"data":"hi"}\n\nevent: stderr\ndata: {"seq":2,"data":"err"}\n\nevent: exit\ndata: {"exit_code":3}\n\n',
            3,
        ),
        ('event: stdout\ndata: {"seq":1,"data":"hi"}\n\n', 1),
        (
            'event: stdout\ndata: {"seq":1,"data":"hi"}\n\nevent: stderr\ndata: {"seq":1,"data":"err"}\n\n',
            1,
        ),
    ],
)
def test_sse_ndjson(mock_api, stream, expected):
    calls = mock_api(
        lambda r: httpx.Response(200, text=stream, headers={"Content-Type": "text/event-stream"})
    )
    result = run("exec", MID, "--command", "cmd", "--stream", "--json")
    assert result.exit_code == expected, result.output
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[0]["event"] == "stdout"
    assert events[-1]["event"] == ("exit" if expected == 3 else "error")
    assert len(calls) == 1


def test_binary_copy(mock_api, tmp_path):
    content = bytes(range(256)) * 500
    checksum = hashlib.sha256(content).hexdigest()
    grants = []

    def handler(request):
        if request.url.path.endswith("/token"):
            body = json.loads(request.content)
            grants.append(body)
            return httpx.Response(
                200,
                json={
                    "url": "https://files.example/capability",
                    "size": len(content),
                    "sha256": checksum,
                },
            )
        assert "authorization" not in request.headers
        if request.method == "POST":
            assert content in request.read()
            assert request.headers["content-type"].startswith("multipart/form-data")
            return httpx.Response(200, json={"size": len(content), "sha256": checksum})
        return httpx.Response(200, content=content)

    mock_api(handler)
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    target = tmp_path / "target.bin"
    assert run("cp", str(source), MID + ":a.bin", "--json").exit_code == 0
    assert run("cp", MID + ":a.bin", str(target), "--json").exit_code == 0
    assert target.read_bytes() == content
    assert run("cp", MID + ":a.bin", str(target), "--json").exit_code == 2
    assert grants[0]["overwrite"] is False
    assert grants[0]["sha256"] == checksum


def test_checksum_failure_preserves_destination(mock_api, tmp_path):
    def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200, json={"url": "https://files.example/cap", "size": 1, "sha256": "bad"}
            )
        return httpx.Response(200, content=b"a")

    mock_api(handler)
    target = tmp_path / "file"
    target.write_text("original")
    result = run("cp", MID + ":a", str(target), "--overwrite", "--json")
    assert result.exit_code == 1
    assert target.read_text() == "original"
    assert not list(tmp_path.glob(".qsp-download-*"))


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "a/../b", "a\x00b", "x" * 256])
def test_workspace_escape(path):
    with pytest.raises(MachineError):
        workspace_path(path)


def test_no_symlink_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("QSP_MACHINES_KEY")
    folder = tmp_path / ".config/quicksilverpro"
    folder.mkdir(parents=True)
    target = tmp_path / "secret"
    target.write_text('{"machines":{"key":"qsm_hidden"}}')
    (folder / "machines.json").symlink_to(target)
    assert run("ls", "--json").exit_code == 2


def test_link_defaults_no_browser(mock_api, monkeypatch):
    def forbidden(*args):
        raise AssertionError("browser not requested")

    monkeypatch.setattr("webbrowser.open", forbidden)
    calls = mock_api(
        lambda r: httpx.Response(
            200, json={"url": "https://quicksilverpro.io/dashboard", "access": "owner"}
        )
    )
    result = run("open", MID, "--json")
    assert result.exit_code == 0
    assert json.loads(calls[0].content)["mode"] == "owner"
    assert "dashboard" not in result.stderr


@pytest.mark.parametrize(
    "command,action", [("pause", "pause"), ("resume", "resume"), ("rm", "delete")]
)
def test_lifecycle_exact_action(mock_api, command, action):
    calls = mock_api(lambda r: httpx.Response(202, json={"state": "Deleting"}))
    result = run(command, MID, *(["--yes"] if command == "rm" else []), "--json")
    assert result.exit_code == 0
    assert calls[0].url.path.endswith("/action/" + action)
    assert json.loads(calls[0].content) == {}


def test_installed_entrypoint_propagates_exit(monkeypatch):
    import sys

    from quicksilverpro.cli import run as entrypoint

    monkeypatch.setattr(sys, "argv", ["qsp", "box", "get", "invalid", "--json"])
    with pytest.raises(SystemExit) as error:
        entrypoint()
    assert error.value.code == 2


def test_lost_create_reports_request_id_before_failure(mock_api):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={})
        raise httpx.ReadError("lost")

    mock_api(handler)
    result = run("create", "--name", "work", "--json")
    request_id = json.loads(result.stdout)["error"]["request_id"]
    assert request_id in result.stderr


def test_delete_wait_accepts_custom_404_code(mock_api):
    def handler(request):
        return (
            httpx.Response(202, json={"state": "Deleting"})
            if request.method == "POST"
            else httpx.Response(404, json={"error": {"code": "machine_not_found"}})
        )

    mock_api(handler)
    result = run("rm", MID, "--yes", "--wait", "--json")
    assert result.exit_code == 0, result.output


def test_redirect_never_sends_key_to_another_origin(mock_api):
    calls = mock_api(
        lambda r: httpx.Response(307, headers={"Location": "https://other.example/v1/machines"})
    )
    result = run("ls", "--json")
    assert result.exit_code == 1
    assert len(calls) == 1
    assert calls[0].url.host == "api.quicksilverpro.io"


def test_interrupted_sse_closes_stream(mock_api):
    closed = []

    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b'event: stdout\ndata: {"seq":1,"data":"started"}\n\n'
            raise KeyboardInterrupt()

        def close(self):
            closed.append(True)

    calls = mock_api(
        lambda r: httpx.Response(
            200, stream=Interrupted(), headers={"content-type": "text/event-stream"}
        )
    )
    result = run("exec", MID, "--command", "work", "--stream", "--json")
    assert result.exit_code == 130
    assert closed == [True]
    assert len(calls) == 1
    assert json.loads(result.stdout.splitlines()[-1])["error"]["code"] == "interrupted"


def test_stream_http_permission_error_is_json(mock_api):
    class Denied(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"error":{"code":"denied"}}'

    mock_api(lambda r: httpx.Response(403, stream=Denied()))
    result = run("exec", MID, "--command", "work", "--stream", "--json")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["status_code"] == 403
