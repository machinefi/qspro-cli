import json

import httpx
import pytest

pytest.importorskip("mcp")
import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from quicksilverpro.machines import MachineError
from quicksilverpro.mcp import SCHEMAS, create_server, dispatch

MID = "abcdef012345"


def test_sdk_tool_calls_and_schemas(mock_api):
    calls = mock_api(lambda r: httpx.Response(200, json={"machines": [], "exit_code": 2}))

    async def scenario():
        async with create_connected_server_and_client_session(create_server()) as session:
            tools = (await session.list_tools()).tools
            assert len(tools) == len(SCHEMAS) == 12
            assert all(
                t.inputSchema["additionalProperties"] is False and t.outputSchema for t in tools
            )
            assert next(t for t in tools if t.name == "box_delete").annotations.destructiveHint
            assert next(t for t in tools if t.name == "box_list").annotations.readOnlyHint
            result = await session.call_tool("box_list", {})
            assert not result.isError
            assert result.structuredContent["result"]["machines"] == []
            result = await session.call_tool("box_exec", {"mid": MID, "argv": ["false"]})
            assert result.isError
            assert result.structuredContent["kind"] == "guest_exit"
            result = await session.call_tool(
                "box_delete", {"mid": MID, "confirm_mid": "000000000000"}
            )
            assert result.isError
            assert result.structuredContent["error"]["code"] == "confirmation_required"
            result = await session.call_tool(
                "box_exec", {"mid": MID, "argv": ["x"], "command": "x"}
            )
            assert result.isError
            assert result.structuredContent["error"]["code"] == "invalid_arguments"

    anyio.run(scenario)
    assert len(calls) == 2


@pytest.mark.parametrize(
    "name,args",
    [
        ("box_upload", {"mid": MID, "path": "x", "data_base64": "?"}),
        ("box_list", {"unexpected": True}),
        ("box_get", {"mid": "foreign"}),
        ("box_open", {"mid": MID, "expires_in_seconds": 901}),
    ],
)
def test_bad_inputs(name, args):
    with pytest.raises(MachineError):
        dispatch(name, args)


def test_real_stdio_protocol_no_credentials(tmp_path):
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def scenario():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "quicksilverpro", "mcp"],
            env={
                "HOME": str(tmp_path),
                "QSP_CONFIG_DIR": str(tmp_path / "config"),
                "QSP_MACHINES_KEY": "",
            },
        )
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                assert len((await session.list_tools()).tools) == 12
                result = await session.call_tool("box_list", {})
                assert result.isError
                assert result.structuredContent["error"]["code"] == "auth_required"

    anyio.run(scenario)


def test_sdk_cancellation_closes_api_transport(monkeypatch):
    import threading

    import quicksilverpro.mcp as module

    started, stopped = threading.Event(), threading.Event()

    class BlockingClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def list(self):
            started.set()
            assert stopped.wait(3), "cancel did not close the transport"
            return {"machines": []}

        def cancel(self):
            stopped.set()

    monkeypatch.setattr(module, "MachinesClient", BlockingClient)

    async def scenario():
        async with create_connected_server_and_client_session(create_server()) as session:
            from mcp import types
            from mcp.shared.exceptions import McpError

            async with anyio.create_task_group() as tasks:
                request_id = session._request_id

                async def call():
                    try:
                        await session.call_tool("box_list", {})
                    except McpError:
                        pass  # Expected cancelled-request response.

                tasks.start_soon(call)
                assert await anyio.to_thread.run_sync(lambda: started.wait(1))
                await session.send_notification(
                    types.ClientNotification(
                        types.CancelledNotification(
                            params=types.CancelledNotificationParams(
                                requestId=request_id, reason="user cancelled"
                            )
                        )
                    )
                )
                with anyio.fail_after(2):
                    while not stopped.is_set():
                        await anyio.sleep(0.01)
                tasks.cancel_scope.cancel()

    anyio.run(scenario)
    assert stopped.is_set()


def test_all_remaining_tools_use_public_api(mock_api):
    import base64
    import hashlib

    data = b"\x00\xffguest data"
    checksum = hashlib.sha256(data).hexdigest()

    def handler(request):
        path = request.url.path
        if request.url.host == "files.example":
            assert "authorization" not in request.headers
            return (
                httpx.Response(200, content=data)
                if request.method == "GET"
                else httpx.Response(200, json={"size": len(data), "sha256": checksum})
            )
        if path.endswith("/token"):
            return httpx.Response(
                200,
                json={"url": "https://files.example/cap", "size": len(data), "sha256": checksum},
            )
        if path.endswith("/links"):
            assert json.loads(request.content)["mode"] == "owner"
            return httpx.Response(
                200, json={"url": "https://quicksilverpro.io/dashboard", "access": "owner"}
            )
        if path.endswith("/files"):
            return httpx.Response(200, json={"entries": []})
        return httpx.Response(
            200, json={"machine": {"id": MID, "state": "Running", "guest_ready": True}}
        )

    calls = mock_api(handler)
    assert (
        dispatch(
            "box_create",
            {
                "name": "work",
                "request_id": "test-create-request-0001",
                "wait_seconds": 1,
                "accept_spend": True,
            },
        )["owner_link"]["access"]
        == "owner"
    )
    assert dispatch("box_get", {"mid": MID})["machine"]["id"] == MID
    assert dispatch("box_wait", {"mid": MID})["machine"]["guest_ready"]
    assert dispatch("box_files_list", {"mid": MID})["entries"] == []
    assert (
        dispatch(
            "box_upload",
            {"mid": MID, "path": "a.bin", "data_base64": base64.b64encode(data).decode()},
        )["sha256"]
        == checksum
    )
    assert (
        base64.b64decode(dispatch("box_download", {"mid": MID, "path": "a.bin"})["data_base64"])
        == data
    )
    for action in ("pause", "resume", "delete"):
        arguments = {"mid": MID, **({"confirm_mid": MID} if action == "delete" else {})}
        if action == "resume":
            arguments["accept_spend"] = True
        dispatch("box_" + action, arguments)
        assert calls[-1].url.path.endswith("/action/" + action)
    assert dispatch("box_open", {"mid": MID})["access"] == "owner"


def test_mcp_large_files_require_cli(mock_api):
    import base64

    with pytest.raises(MachineError) as error:
        dispatch(
            "box_upload",
            {
                "mid": MID,
                "path": "a.bin",
                "data_base64": base64.b64encode(b"x" * (256 * 1024 + 1)).decode(),
            },
        )
    assert error.value.code == "use_cli_cp"
    mock_api(
        lambda r: httpx.Response(
            200,
            json={"url": "https://files.example/cap", "size": 256 * 1024 + 1, "sha256": "unused"},
        )
    )
    with pytest.raises(MachineError) as error:
        dispatch("box_download", {"mid": MID, "path": "large.bin"})
    assert error.value.code == "use_cli_cp"


@pytest.mark.parametrize("approval", [{}, {"accept_spend": False}, {"accept_spend": True}])
def test_sdk_resume_requires_explicit_spend_approval(mock_api, approval):
    calls = mock_api(lambda r: httpx.Response(202, json={"state": "Starting"}))

    async def scenario():
        async with create_connected_server_and_client_session(create_server()) as session:
            tool = next(t for t in (await session.list_tools()).tools if t.name == "box_resume")
            assert tool.inputSchema["properties"]["accept_spend"]["default"] is False
            result = await session.call_tool("box_resume", {"mid": MID, **approval})
            if approval.get("accept_spend"):
                assert not result.isError
                assert result.structuredContent["result"]["state"] == "Starting"
            else:
                assert result.isError
                error = result.structuredContent["error"]
                assert error["code"] == "consent_required"
                assert "accept_spend=true" in error["message"]

    anyio.run(scenario)
    assert [(r.method, r.url.path) for r in calls] == (
        [("POST", f"/v1/machines/{MID}/action/resume")] if approval.get("accept_spend") else []
    )


@pytest.mark.parametrize("approval", ["true", 1, None])
def test_mcp_resume_rejects_non_boolean_approval(mock_api, approval):
    calls = mock_api(lambda r: httpx.Response(202, json={}))
    with pytest.raises(MachineError) as error:
        dispatch("box_resume", {"mid": MID, "accept_spend": approval})
    assert error.value.code == "invalid_arguments"
    assert not calls


CREATE = {"name": "work", "request_id": "test-create-request-0001"}


@pytest.mark.parametrize("approval", [{}, {"accept_spend": False}, {"accept_spend": True}])
def test_sdk_create_requires_explicit_spend_approval(mock_api, tmp_path, approval):
    def handler(r):
        if r.method == "POST":
            return httpx.Response(201, json={"machine": {"id": MID}})
        return httpx.Response(200, json={})

    calls = mock_api(handler)

    async def scenario():
        async with create_connected_server_and_client_session(create_server()) as session:
            tool = next(t for t in (await session.list_tools()).tools if t.name == "box_create")
            assert tool.inputSchema["properties"]["accept_spend"]["default"] is False
            assert "accept_spend=true" in tool.description
            result = await session.call_tool("box_create", {**CREATE, **approval})
            if approval.get("accept_spend"):
                assert not result.isError
            else:
                assert result.isError
                error = result.structuredContent["error"]
                assert error["code"] == "consent_required"
                assert "accept_spend=true" in error["message"]

    anyio.run(scenario)
    posts = [r for r in calls if r.method == "POST"]
    assert bool(posts) == bool(approval.get("accept_spend"))
    if posts:
        assert b"accept_spend" not in posts[0].content
    else:
        assert not calls  # rejected before ANY request, including the journal write
        assert not list(tmp_path.rglob("create-requests.json"))


@pytest.mark.parametrize("approval", ["true", 1, None])
def test_mcp_create_rejects_non_boolean_approval(mock_api, approval):
    calls = mock_api(lambda r: httpx.Response(201, json={}))
    with pytest.raises(MachineError) as error:
        dispatch("box_create", {**CREATE, "accept_spend": approval})
    assert error.value.code == "invalid_arguments"
    assert not calls
