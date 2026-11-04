"""Consent-driven local installation; owns only hashed assets and namespaced MCP entries."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

import click
import tomlkit

from . import __version__
from .configedit import get_entry, set_entry
from .machines import (
    MachineError,
    MachinesClient,
    atomic_write,
    config_dir,
    read_json,
    resolve_key,
    safe_path,
    store_key,
)

AGENTS = ("claude", "codex", "cursor", "opencode", "gemini", "antigravity")


def paths():
    home = Path.home()
    opencode = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "opencode"
    candidates = [opencode / "opencode.jsonc", opencode / "opencode.json"]
    existing = [p for p in candidates if p.exists()]
    if len(existing) > 1:
        raise MachineError(
            "config_conflict",
            "Both OpenCode JSON and JSONC configs exist; resolve precedence before setup.",
            2,
        )
    return {
        "claude": (home / ".claude/skills", home / ".claude.json", "mcpServers"),
        "codex": (
            home / ".agents/skills",
            Path(os.environ.get("CODEX_HOME", home / ".codex")) / "config.toml",
            "mcp_servers",
        ),
        "cursor": (None, home / ".cursor/mcp.json", "mcpServers"),
        "opencode": (home / ".agents/skills", existing[0] if existing else candidates[1], "mcp"),
        "gemini": (home / ".gemini/config/skills", None, None),
        "antigravity": (None, None, None),
    }


def discovery():
    result = {}
    for agent, (skill, config, section) in paths().items():
        result[agent] = {
            "executable": shutil.which("claude" if agent == "claude" else agent),
            "skill_present": bool(skill and (skill / "quicksilver-machines/SKILL.md").exists()),
            "config_present": bool(config and config.exists()),
            "discovery_verified": False,
        }
    return result


def digest(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode()).hexdigest()


def entry_digest(entry):
    return digest(json.dumps(entry, sort_keys=True))


def consent(message, yes=False):
    if yes:
        return True
    if not sys.stdin.isatty():
        raise MachineError(
            "consent_required",
            "Noninteractive setup requires explicit options and --yes; use --dry-run to preview.",
            2,
        )
    return click.confirm(message, default=False)


def backup_write(path, text):
    path = safe_path(path)
    if path.exists():
        original = path.read_bytes()
        backup = (
            config_dir() / "setup-backups" / (digest(str(path))[:12] + "-" + digest(original)[:12])
        )
        if not backup.exists():
            atomic_write(backup, original, private=True)
    atomic_write(path, text)


def config_read(path, section):
    path = safe_path(path)
    text = path.read_text() if path.exists() else ("" if section == "mcp_servers" else "{}\n")
    if section == "mcp_servers":
        try:
            doc = tomlkit.parse(text)
            entry = doc.get(section, {}).get("quicksilver")
            return text, entry.unwrap() if entry is not None else None
        except Exception:
            raise MachineError("invalid_config", "Cannot edit malformed TOML configuration.", 2)
    return text, get_entry(text, section, "quicksilver")


def config_change(path, section, text, entry):
    if section == "mcp_servers":
        doc = tomlkit.parse(text)
        if entry is None:
            del doc[section]["quicksilver"]
        else:
            if section not in doc:
                doc[section] = tomlkit.table()
            doc[section]["quicksilver"] = entry
        output = tomlkit.dumps(doc)
    else:
        output = set_entry(text, section, "quicksilver", entry)
    backup_write(path, output)


def persistent_executable():
    # uvx may put its temporary qsp first on PATH. Prefer uv's persistent bin directory.
    if shutil.which("uv"):
        try:
            result = subprocess.run(
                ["uv", "tool", "dir", "--bin"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            candidate = Path(result.stdout.strip()) / ("qsp.exe" if os.name == "nt" else "qsp")
            if candidate.is_file():
                return str(candidate.absolute())
        except (OSError, subprocess.SubprocessError):
            pass
    found = shutil.which("qsp")
    if found and not any(part in ("archive-v0", "archive-v1") for part in Path(found).parts):
        return str(Path(found).absolute())
    raise MachineError(
        "persistent_cli_required",
        "Install a persistent qsp with uv tool install, pipx or pip; then rerun setup.",
        2,
    )


def manifest_path():
    return config_dir() / "setup-manifest.json"


def save_manifest(manifest):
    atomic_write(manifest_path(), json.dumps(manifest, indent=2), private=True)


def uninstall_owned(dry_run=False, remove_credentials=False, remove_cli=False, yes=False):
    manifest = read_json(manifest_path(), {"files": {}, "entries": {}})
    for path_string, owned in list(manifest.get("files", {}).items()):
        path = safe_path(path_string)
        if path.exists() and digest(path.read_bytes()) != owned["hash"]:
            click.echo("Preserving edited asset: " + str(path))
            continue
        click.echo("Remove owned asset: " + str(path))
        if not dry_run:
            if path.exists():
                path.unlink()
            del manifest["files"][path_string]
    for path_string, owned in list(manifest.get("entries", {}).items()):
        path = safe_path(path_string)
        text, entry = config_read(path, owned["section"])
        if entry is not None and entry_digest(entry) != owned["hash"]:
            click.echo("Preserving edited MCP entry: " + str(path))
            continue
        click.echo("Remove owned MCP entry: " + str(path))
        if not dry_run:
            if entry is not None:
                if owned.get("agent") == "claude":
                    run_client(["claude", "mcp", "remove", "--scope", "user", "quicksilver"])
                else:
                    config_change(path, owned["section"], text, None)
            del manifest["entries"][path_string]
    if not dry_run:
        save_manifest(manifest)
    if remove_credentials:
        click.echo(
            "Remove local Machines credential only (remote revocation requires the browser key page)."
        )
        if not dry_run and consent("Delete the locally stored Machines key?", yes):
            path = safe_path(config_dir() / "machines.json")
            if path.exists():
                path.unlink()
            try:
                import keyring

                keyring.delete_password("quicksilverpro-machines", "machines")
            except Exception:
                pass
    if remove_cli:
        click.echo("Persistent CLI removal: uv tool uninstall quicksilverpro")
        if not dry_run and consent("Uninstall the persistent CLI?", yes):
            run_client(["uv", "tool", "uninstall", "quicksilverpro"])


def run_client(args):
    try:
        # Keep client diagnostics out: some clients print their full config including env secrets.
        subprocess.run(
            args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120
        )
    except (OSError, subprocess.SubprocessError):
        raise MachineError(
            "installer_failed",
            "Integration command failed; check the installed client/version and run setup again.",
        )


class SetupGroup(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except MachineError as error:
            click.echo(error.message, err=True)
            ctx.exit(error.exit_code)
        except OSError:
            click.echo("Local setup file operation failed.", err=True)
            ctx.exit(1)


@click.group(
    cls=SetupGroup,
    invoke_without_command=True,
    help="Install persistent qsp, Machines skills and optional local MCP after consent.",
)
@click.option("--dry-run", is_flag=True)
@click.option("--no-skills", is_flag=True)
@click.option("--no-mcp", is_flag=True)
@click.option(
    "--store-key/--no-store-key",
    "store_key_choice",
    default=None,
    help="Separately choose whether to persist an environment-supplied Machines key for GUI clients.",
)
@click.option("--agents", default="claude,codex", help="Comma-separated: " + ", ".join(AGENTS))
@click.option("--install-cli/--no-install-cli", default=None)
@click.option(
    "--yes",
    is_flag=True,
    help="Approve selected local writes; does not grant browser auth, sharing or machine spend.",
)
@click.option("--uninstall", "remove", is_flag=True)
@click.option("--remove-credentials", is_flag=True)
@click.option("--remove-cli", is_flag=True)
@click.option(
    "--create",
    "create_name",
    help="After setup, separately confirm creation of one billable box (TTY required).",
)
@click.pass_context
def setup(
    ctx,
    dry_run,
    no_skills,
    no_mcp,
    store_key_choice,
    agents,
    install_cli,
    yes,
    remove,
    remove_credentials,
    remove_cli,
    create_name,
):
    if ctx.invoked_subcommand:
        return
    if remove:
        if dry_run or consent("Remove only unchanged qsp-owned assets and MCP entries?", yes):
            uninstall_owned(dry_run, remove_credentials, remove_cli, yes)
        return
    selected = list(dict.fromkeys(a.strip() for a in agents.split(",") if a.strip()))
    if not selected or any(a not in AGENTS for a in selected):
        raise click.UsageError("Select known --agents: " + ", ".join(AGENTS))
    locations = paths()
    click.echo("QuickSilver Machines setup " + __version__)
    click.echo(
        "Credential: OS keychain when available, otherwise private machines.json; validate /v1/machines/auth."
    )
    package = (
        "quicksilverpro"
        + ("[mcp,shell,keychain]" if not no_mcp else "[shell,keychain]")
        + "=="
        + __version__
    )
    click.echo("Persistent install: uv tool install " + package)
    for agent in selected:
        skill, config, section = locations[agent]
        if not no_skills and skill:
            click.echo("Skill destination: " + str(skill / "quicksilver-machines"))
        if not no_mcp and config:
            click.echo("MCP entry: " + str(config) + " / " + section + ".quicksilver")
        if agent in ("gemini", "antigravity"):
            click.echo(
                agent
                + ": discovery unverified; use CLI/skill instructions and the manual MCP snippet. No guessed MCP config is written."
            )
    click.echo(
        "Manifest: " + str(manifest_path()) + "; backups: " + str(config_dir() / "setup-backups")
    )
    if create_name:
        click.echo("Optional billable creation requires a separate interactive confirmation.")
    if dry_run:
        return
    # Fail before installation or filesystem mutations in headless, unauthenticated contexts.
    try:
        key = resolve_key()
    except MachineError as error:
        if error.code != "auth_required" or not sys.stdin.isatty():
            raise
        click.echo(
            "Machines keys grant account-wide machine execution/files/lifecycle control. Inference keys do not work."
        )
        if not click.confirm(
            "Open browser sign-in to approve Machines scopes and expiry? Sharing is optional and excluded by default."
        ):
            raise MachineError("auth_required", "Machines-key approval was declined.", 2)
        webbrowser.open("https://pay.quicksilverpro.io/machines/keys")
        key = click.prompt("Paste the separately approved qsm_ key", hide_input=True).strip()
        with MachinesClient(key=key) as client:
            client.auth()
        if not click.confirm("Store this Machines credential locally (keychain/private file)?"):
            raise MachineError("auth_required", "Local credential storage was declined.", 2)
        store_key(key)
    with MachinesClient(key=key) as client:
        client.auth()
    if os.environ.get("QSP_MACHINES_KEY"):
        if store_key_choice is True or (
            store_key_choice is None
            and sys.stdin.isatty()
            and click.confirm(
                "Persist the environment-supplied Machines key locally for GUI agents?",
                default=False,
            )
        ):
            store_key(key)
        else:
            click.echo(
                "Environment key was not stored; agent processes must inherit QSP_MACHINES_KEY."
            )
    if not no_mcp and sys.version_info < (3, 10):
        raise MachineError(
            "mcp_python_required",
            "MCP requires Python >=3.10; rerun with --no-mcp or a newer interpreter.",
            2,
        )
    if install_cli is None:
        install_cli = sys.stdin.isatty()
    if install_cli:
        if not shutil.which("uv"):
            raise MachineError(
                "uv_required",
                "Install uv first, or install persistently with pipx/pip and pass --no-install-cli.",
                2,
            )
        if consent("Run the displayed persistent installation command?", yes):
            run_client(["uv", "tool", "install", package])
    executable = persistent_executable()
    if not no_mcp:
        run_client([executable, "mcp", "--check"])
    manifest = read_json(manifest_path(), {"version": __version__, "files": {}, "entries": {}})
    manifest["version"] = __version__
    manifest["cli"] = {"executable": executable, "version": __version__, "package": package}
    # Record ownership immediately after each successful write, including partial setup.
    resources = importlib.resources.files("quicksilverpro").joinpath(
        "skills", "quicksilver-machines"
    )
    for agent in selected:
        skill, config, section = locations[agent]
        if (
            not no_skills
            and skill
            and consent("Install/update Machines skill at " + str(skill) + "?", yes)
        ):
            for relative in ("SKILL.md", "references/api.md"):
                target = safe_path(skill / "quicksilver-machines" / relative)
                content = resources.joinpath(*relative.split("/")).read_bytes()
                old = manifest["files"].get(str(target))
                if target.exists() and (not old or digest(target.read_bytes()) != old["hash"]):
                    click.echo("Conflict; preserving user-owned/edited skill: " + str(target))
                    continue
                if not target.exists() or target.read_bytes() != content:
                    backup_write(target, content)
                manifest["files"][str(target)] = {"hash": digest(content), "version": __version__}
                save_manifest(manifest)
        if no_mcp or config is None:
            continue
        text, existing = config_read(config, section)
        entry = (
            {"type": "local", "command": [executable, "mcp"], "enabled": True}
            if agent == "opencode"
            else {"command": executable, "args": ["mcp"]}
        )
        if (
            agent == "claude"
            and existing
            and existing.get("command") == executable
            and existing.get("args") == ["mcp"]
            and not existing.get("env")
            and existing.get("type", "stdio") == "stdio"
        ):
            entry = existing
        old = manifest["entries"].get(str(config))
        if existing is not None and (not old or entry_digest(existing) != old["hash"]):
            click.echo("Conflict; preserving user-owned/edited MCP entry: " + str(config))
            continue
        if not consent(
            "Add/update local stdio MCP for "
            + agent
            + " (machine control; credentials resolved by qsp)?",
            yes,
        ):
            continue
        if existing != entry:
            if agent == "claude":
                if not shutil.which("claude"):
                    click.echo(
                        "Claude Code is not installed; deferred. Run claude mcp add --transport stdio --scope user quicksilver -- "
                        + executable
                        + " mcp"
                    )
                    continue
                if config.exists():
                    original = config.read_bytes()
                    backup = (
                        config_dir()
                        / "setup-backups"
                        / (digest(str(config))[:12] + "-" + digest(original)[:12])
                    )
                    if not backup.exists():
                        atomic_write(backup, original, private=True)
                if existing is not None:
                    run_client(["claude", "mcp", "remove", "--scope", "user", "quicksilver"])
                run_client(
                    [
                        "claude",
                        "mcp",
                        "add",
                        "--transport",
                        "stdio",
                        "--scope",
                        "user",
                        "quicksilver",
                        "--",
                        executable,
                        "mcp",
                    ]
                )
                _, entry = config_read(config, section)
                if entry is None:
                    raise MachineError(
                        "client_config_unknown",
                        "Claude did not write the expected user MCP entry; inspect its supported configuration.",
                    )
            else:
                config_change(config, section, text, entry)
        manifest["entries"][str(config)] = {
            "section": section,
            "hash": entry_digest(entry),
            "agent": agent,
            "version": __version__,
        }
        save_manifest(manifest)
    click.echo(
        "Manual stdio MCP snippet (no credentials): "
        + json.dumps({"command": executable, "args": ["mcp"]})
    )
    click.echo(
        "Run qsp box doctor; verify skill discovery and MCP connection in each installed client. Boxes include QSP inference preinstalled."
    )
    if create_name:
        if not sys.stdin.isatty():
            raise MachineError(
                "consent_required",
                "--create requires interactive billable-machine consent; --yes does not authorize spend.",
                2,
            )
        with MachinesClient() as client:
            click.echo(json.dumps(client.list()))
            if click.confirm(
                "Create a billable box under the displayed prices and account limits?",
                default=False,
            ):
                click.echo(json.dumps(client.create(create_name)))


@setup.command("uninstall")
@click.option("--dry-run", is_flag=True)
@click.option("--yes", is_flag=True)
@click.option("--remove-credentials", is_flag=True)
@click.option("--remove-cli", is_flag=True)
def uninstall(dry_run, yes, remove_credentials, remove_cli):
    if dry_run or consent("Remove only unchanged qsp-owned assets and MCP entries?", yes):
        uninstall_owned(dry_run, remove_credentials, remove_cli, yes)
