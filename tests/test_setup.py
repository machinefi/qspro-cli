import json
import sys

import httpx
import pytest
import tomlkit
from click.testing import CliRunner

import quicksilverpro.setup as installer
from quicksilverpro.cli import main
from quicksilverpro.configedit import get_entry, parse, set_entry
from quicksilverpro.machines import config_dir

mcp_installer = pytest.mark.skipif(
    sys.version_info < (3, 10), reason="MCP installer needs Python >=3.10"
)


@pytest.fixture
def installed(monkeypatch, mock_api, tmp_path):
    mock_api(lambda r: httpx.Response(200, json={"scopes": ["machines:read"]}))
    executable = tmp_path / "bin/qsp"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n")
    monkeypatch.setattr(
        installer.shutil, "which", lambda name: str(executable) if name == "qsp" else None
    )
    monkeypatch.setattr(installer, "run_client", lambda args: None)
    return executable


def invoke(*args):
    return CliRunner().invoke(main, ["setup", *args])


def test_dry_run_no_writes_or_auth(monkeypatch, tmp_path):
    monkeypatch.delenv("QSP_MACHINES_KEY")
    result = invoke("--dry-run", "--agents", ",".join(installer.AGENTS))
    assert result.exit_code == 0, result.output
    assert not list(tmp_path.iterdir())
    assert "discovery unverified" in result.output
    assert "config.toml" in result.output


def test_no_tty_auth_fails_before_mutation(monkeypatch, tmp_path):
    monkeypatch.delenv("QSP_MACHINES_KEY")
    result = invoke("--yes", "--no-install-cli")
    assert result.exit_code == 2
    assert not list(tmp_path.iterdir())


@mcp_installer
def test_install_twice_uninstall_preserves_unrelated(installed, tmp_path):
    codex = tmp_path / ".codex/config.toml"
    codex.parent.mkdir()
    original = '# keep this comment\nmodel = "example"\n\n[mcp_servers.other]\ncommand = "other"\n'
    codex.write_text(original)
    cursor = tmp_path / ".cursor/mcp.json"
    cursor.parent.mkdir()
    cursor.write_text('{"theme":"dark","mcpServers":{"other":{"command":"keep"}}}')
    opencode = tmp_path / ".config/opencode/opencode.jsonc"
    opencode.parent.mkdir(parents=True)
    opencode.write_text(
        '{\n// keep this comment\n"theme":"dark",\n"mcp":{ "other": {"type":"remote", "url":"https://example.com"}, },\n}\n'
    )
    args = ("--yes", "--no-install-cli", "--agents", "codex,cursor,opencode,gemini,antigravity")
    first = invoke(*args)
    assert first.exit_code == 0, first.output
    snapshots = {
        p: p.read_bytes() for p in (codex, cursor, opencode, config_dir() / "setup-manifest.json")
    }
    second = invoke(*args)
    assert second.exit_code == 0, second.output
    assert all(p.read_bytes() == content for p, content in snapshots.items())
    assert "// keep this comment" in opencode.read_text()
    assert tomlkit.parse(codex.read_text())["mcp_servers"]["quicksilver"]["command"] == str(
        installed
    )
    assert get_entry(opencode.read_text(), "mcp", "quicksilver")["command"] == [
        str(installed),
        "mcp",
    ]
    assert (tmp_path / ".gemini/config/skills/quicksilver-machines/SKILL.md").exists()
    result = invoke("--uninstall", "--yes")
    assert result.exit_code == 0, result.output
    assert "# keep this comment" in codex.read_text()
    assert "quicksilver" not in tomlkit.parse(codex.read_text()).get("mcp_servers", {})
    assert get_entry(cursor.read_text(), "mcpServers", "other") == {"command": "keep"}
    assert get_entry(opencode.read_text(), "mcp", "other")["url"] == "https://example.com"
    assert "// keep this comment" in opencode.read_text()
    assert not (tmp_path / ".agents/skills/quicksilver-machines/SKILL.md").exists()


@mcp_installer
def test_preserve_edited_assets_and_entries(installed, tmp_path):
    args = ("--yes", "--no-install-cli", "--agents", "codex,cursor")
    assert invoke(*args).exit_code == 0
    skill = tmp_path / ".agents/skills/quicksilver-machines/SKILL.md"
    skill.write_text("user edits")
    config = tmp_path / ".cursor/mcp.json"
    data = json.loads(config.read_text())
    data["mcpServers"]["quicksilver"]["args"] = ["custom"]
    config.write_text(json.dumps(data))
    assert invoke(*args).exit_code == 0
    result = invoke("uninstall", "--yes")
    assert result.exit_code == 0, result.output
    assert skill.read_text() == "user edits"
    assert json.loads(config.read_text())["mcpServers"]["quicksilver"]["args"] == ["custom"]


@mcp_installer
def test_preserve_unowned_identical_entry(installed, tmp_path):
    config = tmp_path / ".cursor/mcp.json"
    config.parent.mkdir()
    text = json.dumps({"mcpServers": {"quicksilver": {"command": str(installed), "args": ["mcp"]}}})
    config.write_text(text)
    result = invoke("--yes", "--no-install-cli", "--no-skills", "--agents", "cursor")
    assert result.exit_code == 0
    assert "Conflict" in result.output
    assert config.read_text() == text
    assert invoke("--uninstall", "--yes").exit_code == 0
    assert config.read_text() == text


@mcp_installer
def test_no_secrets_in_manifest(installed, tmp_path):
    result = invoke("--yes", "--no-install-cli", "--agents", "codex")
    assert result.exit_code == 0
    assert "qsm_" not in (config_dir() / "setup-manifest.json").read_text()
    assert "qsm_" not in (tmp_path / ".codex/config.toml").read_text()


def test_create_requires_distinct_consent(installed):
    result = invoke("--yes", "--no-install-cli", "--no-skills", "--no-mcp", "--create", "work")
    assert result.exit_code == 2
    assert "--yes does not authorize spend" in result.output


@mcp_installer
def test_claude_supported_command(installed, monkeypatch, tmp_path):
    original_which = installer.shutil.which
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: "/bin/claude" if name == "claude" else original_which(name),
    )
    config = tmp_path / ".claude.json"
    config.write_text('{"unrelated":1}')
    commands = []

    def execute(args):
        if "--check" in args:
            return
        commands.append(args)
        text = config.read_text()
        if "add" in args:
            config.write_text(
                set_entry(
                    text,
                    "mcpServers",
                    "quicksilver",
                    {"type": "stdio", "command": str(installed), "args": ["mcp"], "env": {}},
                )
            )
        else:
            config.write_text(set_entry(text, "mcpServers", "quicksilver", None))

    monkeypatch.setattr(installer, "run_client", execute)
    args = ("--yes", "--no-install-cli", "--agents", "claude")
    assert invoke(*args).exit_code == 0
    assert commands[0] == [
        "claude",
        "mcp",
        "add",
        "--transport",
        "stdio",
        "--scope",
        "user",
        "quicksilver",
        "--",
        str(installed),
        "mcp",
    ]
    assert invoke(*args).exit_code == 0
    assert len(commands) == 1
    assert invoke("uninstall", "--yes").exit_code == 0
    assert json.loads(config.read_text())["unrelated"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "{}",
        '{"x":1}',
        '{"mcp":{}}',
        '{"mcp":{"a":1,}}',
        '{"mcp":{"a":1 /*comment*/}}',
        '{"mcp":{ // last comment\n}}',
    ],
)
def test_jsonc_roundtrip(text):
    out = set_entry(text, "mcp", "quicksilver", {"command": ["qsp", "mcp"]})
    assert get_entry(out, "mcp", "quicksilver") == {"command": ["qsp", "mcp"]}
    removed = set_entry(out, "mcp", "quicksilver", None)
    assert get_entry(removed, "mcp", "quicksilver") is None
    assert parse(removed).value.get("x") == parse(text).value.get("x")


def test_private_key_file_fallback(monkeypatch, tmp_path):
    from quicksilverpro.machines import store_key

    monkeypatch.setitem(sys.modules, "keyring", None)
    store_key("qsm_test")
    path = config_dir() / "machines.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_symlink_install_refused(installed, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / ".agents").symlink_to(target)
    result = invoke("--yes", "--no-install-cli", "--agents", "codex", "--no-mcp")
    assert result.exit_code == 2
    assert not list(target.iterdir())


def test_skill_only_install_on_base_python(installed, tmp_path):
    result = invoke("--yes", "--no-install-cli", "--no-mcp", "--agents", "codex")
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".agents/skills/quicksilver-machines/SKILL.md").exists()
    assert not (tmp_path / ".codex/config.toml").exists()


@pytest.mark.parametrize("text", ['{"x":NaN}', '{"mcp": [}', '{"a":1,"a":2}'])
def test_invalid_jsonc_is_safe_error(text):
    from quicksilverpro.machines import MachineError

    with pytest.raises(MachineError) as error:
        parse(text)
    assert error.value.code == "invalid_config"


def test_comment_cannot_change_deleted_property():
    text = '{"mcp":{"quicksilver": /* "other": */ {"command":"qsp"}, "other": {"command":"keep"}}}'
    result = set_entry(text, "mcp", "quicksilver", None)
    assert get_entry(result, "mcp", "other") == {"command": "keep"}


def test_persistent_path_avoids_uvx_cache(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace

    installed_bin = tmp_path / "bin"
    installed_bin.mkdir()
    (installed_bin / "qsp").write_text("qsp")
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: "/bin/uv" if name == "uv" else "/cache/uv/archive-v0/id/bin/qsp",
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=str(installed_bin))
    )
    assert installer.persistent_executable() == str(installed_bin / "qsp")


def test_env_key_storage_requires_explicit_choice(installed, monkeypatch):
    keys = []
    monkeypatch.setattr(installer, "store_key", keys.append)
    args = ("--yes", "--no-install-cli", "--no-skills", "--no-mcp")
    assert invoke(*args).exit_code == 0
    assert keys == []
    assert invoke(*args, "--store-key").exit_code == 0
    assert keys == ["qsm_test_only"]
