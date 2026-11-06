# quicksilverpro (npm launcher)

```bash
npx quicksilverpro setup        # sign in, install the QuickSilver skill + MCP into your agents
npx quicksilverpro box ls       # your cloud dev boxes
```

This package is a thin launcher for the [QuickSilver Pro CLI](https://github.com/machinefi/qspro-cli) (`qsp`), a Python tool. It runs the matching version through [uv](https://docs.astral.sh/uv/) (`uvx`), or pipx if uv is missing.

- One OpenAI-compatible API for frontier and open models: https://quicksilverpro.io
- QuickSilver Machines: cloud dev boxes with Claude Code, Codex, OpenCode and Hermes ready out of the box. Agents ready out of the box. One unified bill. Zero friction.
- Docs: https://quicksilverpro.io/docs/machines/cli/
