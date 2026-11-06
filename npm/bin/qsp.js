#!/usr/bin/env node
// Thin launcher: runs the Python `quicksilverpro` CLI pinned to this package's
// version via uv (uvx), falling back to pipx. No network calls of its own.
"use strict";
const { spawnSync } = require("node:child_process");
const { version } = require("../package.json");

const args = process.argv.slice(2);
const spec = `quicksilverpro==${version}`;

function has(cmd) {
  const probe = spawnSync(process.platform === "win32" ? "where" : "which", [cmd], { stdio: "ignore" });
  return probe.status === 0;
}

function run(cmd, cmdArgs) {
  const res = spawnSync(cmd, cmdArgs, { stdio: "inherit", env: process.env });
  if (res.error) {
    console.error(`quicksilverpro: failed to start ${cmd}: ${res.error.message}`);
    process.exit(1);
  }
  process.exit(res.status === null ? 1 : res.status);
}

if (has("uvx")) {
  run("uvx", ["--from", spec, "qsp", ...args]);
} else if (has("pipx")) {
  run("pipx", ["run", "--spec", spec, "qsp", ...args]);
} else {
  console.error(
    [
      "quicksilverpro needs uv (recommended) or pipx to run the Python CLI.",
      "",
      "Install uv (one line), then re-run this command:",
      process.platform === "win32"
        ? '  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
        : "  curl -LsSf https://astral.sh/uv/install.sh | sh",
      "",
      "Or install directly: pip install quicksilverpro   (or: brew install machinefi/qspro/qspro)",
    ].join("\n"),
  );
  process.exit(127);
}
