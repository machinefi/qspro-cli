"""Agent-friendly command line interface for Machines."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import webbrowser
from contextlib import closing
from pathlib import Path

import click

from .machines import MachineError, MachinesClient, exec_exit, mid_value, safe_path


def envelope(kind, result=None, error=None):
    data = {"version": 1, "kind": kind}
    if result is not None:
        data["result"] = result
    if error is not None:
        data["error"] = error
    return data


def emit(result, as_json=False, kind="result"):
    click.echo(json.dumps(envelope(kind, result) if as_json else result, ensure_ascii=True))


class BoxGroup(click.Group):
    def parse_args(self, ctx, args):
        ctx.meta["box_json"] = "--json" in args
        ctx.meta["box_stream"] = "--stream" in args
        return super().parse_args(ctx, args)

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (MachineError, click.UsageError, click.Abort, KeyboardInterrupt, OSError) as error:
            if isinstance(error, MachineError):
                code, payload = error.exit_code, error.payload()
            elif isinstance(error, click.UsageError):
                code, payload = 2, {"code": "usage_error", "message": error.format_message()}
            elif isinstance(error, (KeyboardInterrupt, click.Abort)):
                code, payload = (
                    130,
                    {
                        "code": "interrupted",
                        "message": "Interrupted; in-flight execution may have an unknown outcome. No retry was attempted.",
                    },
                )
            else:
                code, payload = (
                    1,
                    {"code": "local_error", "message": "Local file or terminal operation failed."},
                )
            if ctx.meta.get("box_json"):
                data = envelope("cli_error", error=payload)
                if ctx.meta.get("box_stream"):
                    data["event"] = "error"
                click.echo(json.dumps(data))
            else:
                click.echo(payload["message"], err=True)
            ctx.exit(code)


@click.group(
    cls=BoxGroup, help="Control QuickSilver boxes with a separate Machines key (beta API)."
)
def box():
    pass


def json_option(fn):
    return click.option("--json", "as_json", is_flag=True, help="Emit a versioned JSON object.")(fn)


def confirm_spend(client, operation, accept_spend):
    # Display effective account policy, without guessing deployed prices.
    policy = client.list()
    click.echo(
        json.dumps(
            {k: policy[k] for k in ("prices", "limits", "retention", "trial") if k in policy}
        ),
        err=True,
    )
    if not accept_spend:
        if not sys.stdin.isatty():
            raise MachineError(
                "consent_required",
                f"Billable {operation} requires interactive approval or --accept-spend.",
                2,
            )
        prompt = "Create a billable box" if operation == "creation" else "Resume billing for this box"
        if not click.confirm(
            prompt + " under the displayed prices and limits?", default=False, err=True
        ):
            raise MachineError("consent_required", f"Billable {operation} was declined.", 2)


@box.command("ls")
@json_option
def listing(as_json):
    with MachinesClient() as client:
        emit(client.list(), as_json)


@box.command()
@click.argument("mid")
@json_option
def get(mid, as_json):
    with MachinesClient() as client:
        emit(client.get(mid), as_json)


@box.command()
@click.option("--name", required=True)
@click.option("--size", type=click.Choice(["2", "4"]), default="2", show_default=True)
@click.option(
    "--request-id", help="Reuse for recovery; use a fresh ID for a new box with the same name/size."
)
@click.option("--wait", "do_wait", is_flag=True)
@click.option("--timeout", type=click.IntRange(1, 180), default=180)
@click.option(
    "--accept-spend",
    is_flag=True,
    help="Explicitly approve billable creation without an interactive prompt.",
)
@json_option
def create(name, size, request_id, do_wait, timeout, accept_spend, as_json):
    with MachinesClient() as client:
        confirm_spend(client, "creation", accept_spend)
        result = client.create(
            name,
            int(size),
            request_id,
            on_request_id=lambda rid: click.echo("Create request ID: " + rid, err=True),
        )
        machine = result.get("machine", result)
        mid = machine.get("id", machine.get("mid"))
        if mid:
            click.echo("Machine ID: " + mid, err=True)
        if do_wait:
            if not mid:
                raise MachineError(
                    "invalid_response",
                    "Create response has no machine ID; recover with the stored request ID.",
                )
            result = {**client.wait(mid, timeout), "request_id": result["request_id"]}
        emit(result, as_json)


@box.command()
@click.argument("mid")
@click.option("--timeout", type=click.IntRange(1, 180), default=180)
@json_option
def wait(mid, timeout, as_json):
    with MachinesClient() as client:
        emit(client.wait(mid, timeout), as_json)


@box.command("exec")
@click.argument("mid")
@click.option("--argv-json")
@click.option("--command", help="Explicitly run guest /bin/bash -lc.")
@click.option("--cwd")
@click.option("--timeout", type=click.IntRange(1, 300), default=30)
@click.option("--stream", is_flag=True)
@json_option
def execute(mid, argv_json, command, cwd, timeout, stream, as_json):
    try:
        argv = json.loads(argv_json) if argv_json is not None else None
    except ValueError:
        raise click.UsageError("--argv-json must be valid JSON.")
    if argv_json is not None and argv is None:
        raise click.UsageError("--argv-json must be an array.")
    with MachinesClient() as client:
        args = dict(argv=argv, command=command, cwd=cwd, timeout_seconds=timeout)
        if stream:
            result = None
            with closing(client.events(mid, **args)) as events:
                for event in events:
                    if as_json:
                        click.echo(json.dumps({"version": 1, "kind": "guest_event", **event}))
                    elif event["event"] in ("stdout", "stderr"):
                        click.echo(event["data"], nl=False, err=event["event"] == "stderr")
                    if event["event"] == "exit":
                        result = event
        else:
            result = client.execute(mid, **args)
            if as_json:
                emit(result, True, "guest_exit")
            else:
                click.echo(result.get("stdout", ""), nl=False)
                click.echo(result.get("stderr", ""), nl=False, err=True)
        if result is None:
            raise MachineError("outcome_unknown", "Missing execution exit evidence.")
        click.get_current_context().exit(exec_exit(result))


@box.command()
@click.argument("source")
@click.argument("destination")
@click.option("--overwrite", is_flag=True)
@json_option
def cp(source, destination, overwrite, as_json):
    def remote(value):
        if ":" not in value:
            return None
        mid, path = value.split(":", 1)
        # Absolute local paths can contain colons; remote syntax must start with a MID.
        if len(mid) == 12 and all(c in "0123456789abcdef" for c in mid):
            return mid, path
        return None

    src, dst = remote(source), remote(destination)
    if bool(src) == bool(dst):
        raise click.UsageError(
            "Copy requires one LOCAL path and one MID:workspace/path; one regular file only."
        )
    with MachinesClient() as client:
        if dst:
            local = Path(source)
            if local.is_symlink() or not stat.S_ISREG(local.stat().st_mode):
                raise MachineError("invalid_file", "Upload requires a regular local file.", 2)
            with local.open("rb") as stream:
                result = client.upload(*dst, stream, overwrite)
        else:
            local = safe_path(Path(destination))
            if local.exists() and (not overwrite or not local.is_file()):
                raise MachineError(
                    "destination_exists",
                    "Destination exists; regular files require --overwrite.",
                    2,
                )
            fd, temporary = tempfile.mkstemp(prefix=".qsp-download-", dir=local.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    result = client.download(*src, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                safe_path(local)
                if overwrite:
                    os.replace(temporary, local)
                else:
                    os.link(temporary, local)  # Atomic no-clobber commit.
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        emit(result, as_json)


@box.command()
@click.argument("mid")
@click.option("--view", type=click.Choice(["desktop", "terminal"]), default="desktop")
@click.option("--share", is_flag=True, help="Explicitly mint a read-only bearer share link.")
@click.option("--browser", is_flag=True)
@click.option("--expires-in-seconds", type=click.IntRange(60, 900), default=300)
@json_option
def open(mid, view, share, browser, expires_in_seconds, as_json):
    with MachinesClient() as client:
        result = client.link(mid, view, "share" if share else "owner", expires_in_seconds)
    if browser:
        webbrowser.open(result["url"])
    if as_json:
        emit(result, True)
    else:
        click.echo(result["url"])


@box.command()
@click.argument("mid")
@json_option
def pause(mid, as_json):
    with MachinesClient() as client:
        emit(client.action(mid, "pause"), as_json)


@box.command()
@click.argument("mid")
@click.option("--wait", "do_wait", is_flag=True)
@click.option("--timeout", type=click.IntRange(1, 180), default=180)
@click.option(
    "--accept-spend",
    is_flag=True,
    help="Explicitly approve resuming billing without an interactive prompt.",
)
@json_option
def resume(mid, do_wait, timeout, accept_spend, as_json):
    mid_value(mid)
    with MachinesClient() as client:
        confirm_spend(client, "resume", accept_spend)
        result = client.action(mid, "resume")
        emit(client.wait(mid, timeout) if do_wait else result, as_json)


@box.command()
@click.argument("mid")
@click.option("--yes", is_flag=True)
@click.option("--wait", "do_wait", is_flag=True)
@click.option("--timeout", type=click.IntRange(1, 180), default=180)
@json_option
def rm(mid, yes, do_wait, timeout, as_json):
    mid_value(mid)
    if not yes:
        if not sys.stdin.isatty():
            raise click.UsageError("Noninteractive deletion requires --yes.")
        if not click.confirm("Permanently delete box " + mid + " and its files?", err=True):
            raise click.Abort()
    with MachinesClient() as client:
        result = client.action(mid, "delete")
        emit(client.wait(mid, timeout, deleted=True) if do_wait else result, as_json)


@box.command()
@click.argument("mid")
@json_option
def shell(mid, as_json):
    if as_json or not sys.stdin.isatty() or not sys.stdout.isatty():
        raise click.UsageError("shell requires a TTY and does not support --json.")
    from .terminal import bridge

    with MachinesClient() as client:
        bridge(client.grant(mid, "terminal")["url"])


@box.command()
@click.option("--mid", help="Also check an existing box's readiness without changing its state.")
@json_option
def doctor(mid, as_json):
    from .setup import discovery

    with MachinesClient() as client:
        result = {
            "auth": client.auth(),
            "endpoint": client.origin,
            "agents": discovery(),
            "note": "Config presence is not proof of discovery; verify in the installed client's MCP/skills UI.",
        }
        if mid:
            result["machine"] = client.get(mid)
        emit(result, as_json)
