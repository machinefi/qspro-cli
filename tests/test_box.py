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


def runner():
    """Separate stderr on every Click version (8.1 mixes it by default)."""
    return (
        CliRunner(mix_stderr=False)
        if "mix_stderr" in inspect.signature(CliRunner).parameters
        else CliRunner()
    )


def run(*args):
    return runner().invoke(main, ["box", *args])


def all_output(result):
    """stdout + stderr on every Click version (older Click keeps stderr separate)."""
    try:
        err = result.stderr
    except ValueError:
        err = ""
    return result.output + (err if err not in result.output else "")


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
    assert "qsm_secret" not in all_output(result)
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
    assert "qsm_secret" not in all_output(result)


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
    first = run("create", "--name", "work", "--accept-spend", "--json")
    second = run("create", "--name", "work", "--accept-spend", "--json")
    assert first.exit_code == 1
    assert second.exit_code == 0
    assert received[0] == received[1]
    journal = tmp_path / ".config/quicksilverpro/create-requests.json"
    assert journal.stat().st_mode & 0o777 == 0o600
    assert journal.parent.stat().st_mode & 0o777 == 0o700
    assert "qsm_" not in journal.read_text()


def test_wait_requires_guest_ready(mock_api, monkeypatch):
    states = iter(
        [
            {"state": "Running", "guest_ready": False},
            {"state": "Running", "guest_ready": True},
        ]
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
                    "host": "https://files.example",
                    "token": "cap&?",
                    "resource": body["resource"],
                    "expires_in": 60,
                    "mode": "cli",
                },
            )
        assert "authorization" not in request.headers
        assert request.url.path == f"/files/{MID}/" + (
            "upload" if request.method == "POST" else "download"
        )
        assert dict(request.url.params) == {"path": "a.bin", "token": "cap&?"}
        if request.method == "POST":
            assert content in request.read()
            assert request.headers["content-type"].startswith("multipart/form-data")
            return httpx.Response(200, json={"size": len(content), "sha256": checksum})
        return httpx.Response(
            200, content=content, headers={"X-Checksum-SHA256": checksum}
        )

    mock_api(handler)
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    target = tmp_path / "target.bin"
    assert run("cp", str(source), MID + ":a.bin", "--json").exit_code == 0
    assert run("cp", MID + ":a.bin", str(target), "--json").exit_code == 0
    assert target.read_bytes() == content
    assert run("cp", MID + ":a.bin", str(target), "--json").exit_code == 2
    assert grants[0]["overwrite"] is False
    assert grants[0] == {
        "purpose": "upload",
        "resource": "a.bin",
        "size": len(content),
        "overwrite": False,
        "sha256": checksum,
    }
    assert grants[1] == {"purpose": "download", "resource": "a.bin"}


def test_checksum_failure_preserves_destination(mock_api, tmp_path):
    def handler(request):
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200,
                json={"host": "https://files.example", "token": "cap", "resource": "a"},
            )
        return httpx.Response(
            200, content=b"a", headers={"X-Checksum-SHA256": "0" * 64}
        )

    mock_api(handler)
    target = tmp_path / "file"
    target.write_text("original")
    result = run("cp", MID + ":a", str(target), "--overwrite", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "checksum_mismatch"
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
    flags = {"rm": ["--yes"], "resume": ["--accept-spend"]}.get(command, [])
    result = run(command, MID, *flags, "--json")
    assert result.exit_code == 0
    assert calls[-1].url.path.endswith("/action/" + action)
    assert json.loads(calls[-1].content) == {}


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
    result = run("create", "--name", "work", "--accept-spend", "--json")
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
    assert calls[0].url.host == "pay.quicksilverpro.io"


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


@pytest.mark.parametrize("command", [("create", "--name", "work"), ("resume", MID)])
@pytest.mark.parametrize("as_json", [False, True])
def test_headless_requires_spend_flag(mock_api, monkeypatch, tmp_path, as_json, command):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    calls = mock_api(lambda r: httpx.Response(200, json={"prices": {"2": 0.02}}))
    result = run(*command, *(["--json"] if as_json else []))
    assert result.exit_code == 2, result.output
    assert "--accept-spend" in all_output(result)
    assert [r.method for r in calls] == ["GET"]
    assert not (tmp_path / ".config/quicksilverpro/create-requests.json").exists()
    if as_json:
        assert json.loads(result.stdout)["error"]["code"] == "consent_required"


@pytest.mark.parametrize(
    "command,prompt",
    [
        (("create", "--name", "work"), "Create a billable"),
        (("resume", MID), "Resume billing"),
    ],
)
@pytest.mark.parametrize("answer,exit_code", [("y", 0), ("n", 2), ("", 2)])
def test_interactive_consent(mock_api, monkeypatch, answer, exit_code, command, prompt):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "quicksilverpro.box.sys",
        SimpleNamespace(stdin=SimpleNamespace(isatty=lambda: True)),
    )
    calls = mock_api(
        lambda r: httpx.Response(
            200 if r.method == "GET" else 201,
            json={"prices": {"2": 0.02}, "limits": {"stored": 1}}
            if r.method == "GET" else {"machine": {"id": MID}},
        )
    )
    result = runner().invoke(
        main, ["box", *command, "--json"], input=answer + "\n"
    )
    assert result.exit_code == exit_code, result.output
    assert [r.method for r in calls] == (["GET", "POST"] if exit_code == 0 else ["GET"])
    assert result.stderr.index('"prices"') < result.stderr.index(prompt)
    assert result.stderr.index('"limits"') < result.stderr.index(prompt)
    # Click 8.1's runner echoes prompt input to stdout even with err=True.
    stdout = result.stdout.removeprefix(f" {answer}\n")
    assert len(stdout.splitlines()) == 1
    assert json.loads(stdout)["kind"] == ("result" if exit_code == 0 else "cli_error")


@pytest.mark.parametrize("command", [("create", "--name", "work"), ("resume", MID)])
def test_explicit_spend_flag_skips_prompt(mock_api, monkeypatch, command):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Explicit spend approval must not prompt")

    monkeypatch.setattr("click.confirm", forbidden)
    calls = mock_api(lambda r: httpx.Response(200, json={"machine": {"id": MID}}))
    result = run(*command, "--accept-spend", "--json")
    assert result.exit_code == 0, result.output
    assert [r.method for r in calls] == ["GET", "POST"]


def test_resume_approved_waits_after_action(mock_api):
    def handler(request):
        if request.url.path == "/v1/machines":
            return httpx.Response(200, json={"prices": {"2": 0.02}, "limits": {"running": 1}})
        return httpx.Response(200, json={"state": "Running", "guest_ready": True})

    calls = mock_api(handler)
    result = run("resume", MID, "--accept-spend", "--wait", "--timeout", "1", "--json")
    assert result.exit_code == 0, result.output
    assert [(r.method, r.url.path) for r in calls] == [
        ("GET", "/v1/machines"),
        ("POST", f"/v1/machines/{MID}/action/resume"),
        ("GET", f"/v1/machines/{MID}"),
    ]
    assert json.loads(result.stderr)["limits"] == {"running": 1}
    assert json.loads(result.stdout)["result"]["guest_ready"] is True


def test_resume_policy_failure_never_mutates(mock_api):
    calls = mock_api(lambda r: httpx.Response(503, json={}))
    result = run("resume", MID, "--accept-spend", "--json")
    assert result.exit_code == 1
    assert [r.method for r in calls] == ["GET"]


@pytest.mark.parametrize(
    "status,path_args",
    [
        (404, ("ls",)),
        (401, ("ls",)),
        (401, ("get", MID)),
        (404, ("create", "--name", "w", "--accept-spend")),
    ],
)
def test_agent_access_disabled_json(mock_api, status, path_args):
    mock_api(lambda r: httpx.Response(status, json={"detail": "Not Found"}))
    result = run(*path_args, "--json")
    assert result.exit_code == 2
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "agent_access_disabled"
    assert "agent access isn't enabled for this account yet" in error["message"]


def test_agent_access_disabled_human(mock_api):
    mock_api(lambda r: httpx.Response(404, text="nope"))
    result = run("ls")
    assert result.exit_code == 2
    assert (
        "QuickSilver Machines agent access isn't enabled for this account yet"
        in result.stderr
    )
    assert "Traceback" not in result.output


def test_machine_404_and_coded_errors_not_reclassified(mock_api):
    mock_api(
        lambda r: httpx.Response(404, json={"error": {"code": "machine_not_found"}})
    )
    assert (
        json.loads(run("get", MID, "--json").stdout)["error"]["code"]
        == "machine_not_found"
    )
    mock_api(lambda r: httpx.Response(404, json={}))
    assert json.loads(run("get", MID, "--json").stdout)["error"]["code"] == "http_404"
    mock_api(lambda r: httpx.Response(401, json={"error": {"code": "invalid_key"}}))
    assert json.loads(run("ls", "--json").stdout)["error"]["code"] == "invalid_key"


@pytest.mark.parametrize("stream", [False, True])
def test_ready_retry_then_exec(mock_api, monkeypatch, stream):
    sleeps = []
    monkeypatch.setattr("quicksilverpro.machines.time.sleep", sleeps.append)
    responses = iter(
        [
            httpx.Response(409, json={"detail": "Machine is not ready."}),
            httpx.Response(200, text='event: exit\ndata: {"exit_code":0}\n\n')
            if stream
            else httpx.Response(200, json={"exit_code": 0}),
        ]
    )
    calls = mock_api(lambda r: next(responses))
    args = (
        ["exec", MID, "--json"]
        + (["--stream"] if stream else [])
        + ["--", "printf", "%s", "--json", "a b", "$(false)"]
    )
    result = run(*args)
    assert result.exit_code == 0, all_output(result)
    assert len(calls) == 2 and sleeps == [1]
    assert json.loads(calls[-1].content)["argv"] == [
        "printf",
        "%s",
        "--json",
        "a b",
        "$(false)",
    ]


@pytest.mark.parametrize(
    "message",
    [
        "Guest execution could not start. Check status before retrying.",
        "Machine generation changed.",
        "Execution outcome unknown.",
        "not ready after execution started",
    ],
)
def test_exec_conflict_never_retried(mock_api, message):
    calls = mock_api(lambda r: httpx.Response(409, json={"detail": message}))
    result = run("exec", MID, "--json", "--", "true")
    assert json.loads(result.stdout)["error"]["message"] == message
    assert len(calls) == 1


def test_exec_readiness_retry_bounded(mock_api, monkeypatch):
    now = [0]

    def sleep(delay):
        now[0] += delay

    monkeypatch.setattr("quicksilverpro.machines.time.monotonic", lambda: now[0])
    monkeypatch.setattr("quicksilverpro.machines.time.sleep", sleep)
    calls = mock_api(
        lambda r: httpx.Response(409, json={"detail": "Machine is not ready."})
    )
    result = run("exec", MID, "--json", "--", "true")
    assert result.exit_code == 1
    assert now[0] == 30
    assert len(calls) <= 10


@pytest.mark.parametrize(
    "payload",
    [
        {"detail": "Choose a single file."},
        {"error": {"code": "invalid_request", "message": "Choose a single file."}},
    ],
)
def test_server_error_message(mock_api, payload):
    mock_api(lambda r: httpx.Response(400, json=payload))
    assert "Choose a single file." in all_output(run("get", MID))
    assert (
        json.loads(run("get", MID, "--json").stdout)["error"]["message"]
        == "Choose a single file."
    )


def test_large_upload_rejected_before_token(mock_api, tmp_path):
    calls = mock_api(lambda r: pytest.fail("must not request a token"))
    source = tmp_path / "large"
    with source.open("wb") as out:
        out.truncate(64 * 1024 * 1024 + 1)
    result = run("cp", str(source), MID + ":large", "--json")
    assert result.exit_code == 2
    assert "64 MiB" in result.stdout
    assert not calls


def test_box_help_and_table(mock_api):
    from quicksilverpro.box import box

    assert all(command.get_short_help_str() for command in box.commands.values())
    mock_api(
        lambda r: httpx.Response(
            200,
            json={
                "machines": [
                    {
                        "id": MID,
                        "name": "work",
                        "state": "Running",
                        "size": 2,
                        "hours_month": 1.25,
                    }
                ],
                "plan": "paid",
                "available_balance": 5,
                "limit": 3,
                "monthly_hours": {"used": 1.25, "cap": 100},
            },
        )
    )
    result = run("ls")
    assert result.exit_code == 0
    assert "HOURS THIS MONTH" in result.stdout and "1.25" in result.stdout
    assert "Account: paid" in result.stdout and "boxes 1/3" in result.stdout


def test_exec_modes_exclusive(mock_api):
    calls = mock_api(lambda r: pytest.fail("invalid argv must not execute"))
    assert run("exec", MID, "--command", "true", "--", "false").exit_code == 2
    assert not calls


@pytest.mark.parametrize("stream", [False, True])
def test_unknown_outcome_code_overrides_readiness_message(mock_api, stream):
    calls = mock_api(lambda r: httpx.Response(409, json={
        "detail": "Machine is not ready.",
        "error": {"code": "exec_outcome_unknown", "retryable": False},
    }))
    args = ["exec", MID, "--json"] + (["--stream"] if stream else [])
    result = run(*args, "--", "true")
    assert result.exit_code == 1
    assert len(calls) == 1


def test_guest_json_flag_does_not_change_error_format(mock_api):
    mock_api(lambda r: httpx.Response(400, json={"detail": "Guest rejected."}))
    result = run("exec", MID, "--", "echo", "--json")
    assert result.exit_code == 1
    assert result.stderr.strip() == "Guest rejected."
    assert not result.stdout
