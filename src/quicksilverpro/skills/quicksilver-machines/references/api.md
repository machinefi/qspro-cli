# Machines beta client contract (0.5.0)

Machines API origin is configured separately using `QSP_MACHINES_URL` (HTTPS origin only), default `https://pay.quicksilverpro.io`. No network service is launched by the CLI or MCP server. Commands do not implicitly resume or create replacement machines.

| Operation | Public request |
|---|---|
| Validate key | `GET /v1/machines/auth` |
| List/create | `GET/POST /v1/machines` |
| Get/wait | `GET /v1/machines/{mid}` |
| Pause/resume/delete | `POST /v1/machines/{mid}/action/{pause,resume,delete}`, `{}` |
| Exec | `POST /v1/machines/{mid}/exec` |
| Files | `GET /v1/machines/{mid}/files?path=.` |
| Transfer/terminal grant | `POST /v1/machines/{mid}/token` |
| Human link | `POST /v1/machines/{mid}/links` |

Create: `{name,size:2|4,request_id}`. ID is 16..64 letters/digits/hyphens. The local intent journal stores no credentials, is account/origin-specific, and is written before the request. Repeating name/size reuses its request ID, including across process restarts. Supply a fresh explicit ID to intentionally create another box with identical parameters. No unknown-outcome mutation is retried automatically.

Machine responses may be the public machine object or `{machine:{...},...metadata}`; machine ID is `id` (adapter also accepts `mid`), lifecycle is `state` (adapter also accepts `status`). Readiness requires `Running` plus boolean `guest_ready:true`. Deletion wait requires a `Deleted` tombstone or HTTP 404, never merely an accepted deletion. Wait uses 1..5-second jittered backoff and an absolute deadline.

Exec: exactly one of `argv` (1..256 strings) or `command` (explicit guest Bash), optional `cwd`, `timeout_seconds:1..300` (default 30). JSON result: `{exec_id,stdout,stderr,exit_code,signal,timed_out,truncated,invalid_utf8,duration_ms}`. Never infer success without exit evidence. Combined output is <=1 MiB. CLI streaming requests `Accept:text/event-stream`; each SSE frame uses `event:stdout|stderr`, JSON `data:{seq:int,data:string}`. Sequence increases across both streams. `event:exit` contains the terminal result metadata; `event:error` is failure. The CLI closes interrupted streams; backend cancellation must terminate the whole guest job tree. NDJSON wraps these events with `{version:1,kind:"guest_event",event,...}`; CLI failure uses `{version:1,kind:"cli_error",event:"error",error}`.

## Adapter assumptions requiring backend fixture agreement

The design defines grant security and limits but does not fully specify its wire fields. `MachinesClient.grant`, `upload`, `download`, and `terminal.bridge` isolate these beta assumptions:

- Token input `{purpose:"upload"|"download"|"terminal",client_mode:"cli",...}`. File fields: normalized `path`, `max_bytes`; upload additionally `sha256` and `overwrite`.
- Response `url` is the complete short-lived capability URL (`https` for transfers, `wss` for terminal). No account authorization is forwarded, and redirects are refused. The backend chooses a trusted public data-plane URL; no URL-fetch tool is exposed.
- Upload is a streaming multipart POST with field `file`, matching the existing data-plane upload contract. Success returns `{size,sha256}`. Download grant includes `{size,sha256}`, and the URL accepts GET. Both require exact byte/checksum verification. Local download uses a temporary file and atomic commit with explicit overwrite. Limits: 64 MiB CLI, 256 KiB MCP, 64 KiB chunks, 10-second I/O timeout and 300-second transfer deadline.
- Terminal accepts binary stdin and returns binary or text stdout. Resize uses JSON `{type:"resize",cols,rows}`. Ctrl-C is forwarded as raw byte 0x03; Ctrl-] disconnects. Terminal state and signal handlers are restored on all exit paths. Shell is POSIX-only, with optional `shell` extra.

Do not claim deployed compatibility until these adapters pass an isolated backend fixture and real terminal smoke test.

## Local MCP

Install `quicksilverpro[mcp]==0.5.0` with Python >=3.10; SDK pinned to `mcp==1.25.0` (SDK negotiates supported protocol versions). `qsp mcp` is stdio only and resolves credentials itself. No secrets in agent config. `qsp mcp --check` verifies the installation without starting stdio. Protocol cancellation closes the active API/file transports; backend disconnect handling must cancel the guest job tree. Tools: `box_create/list/get/wait/exec/files_list/upload/download/pause/resume/delete/open`.

Input schemas and common output envelope schema are published in `quicksilverpro.mcp`. Inputs forbid unknown properties, require MID `^[a-f0-9]{12}$`, and expand all aliases. CLI create/resume display effective prices and limits on stderr and require interactive confirmation (default no) or `--accept-spend`; headless calls without the flag fail with exit 2 and `consent_required`. Agents must show the returned prices/limits and obtain explicit user approval before passing the flag. MCP `box_create` and `box_resume` require `accept_spend:true`; call `box_list`, present its prices/limits and obtain user approval before setting it. Missing or false approval returns `isError:true` with `consent_required` without creating or resuming (nothing is written to the create-request journal). If the account lacks Machines agent access (HTTP 401, or 404 on `/v1/machines`/`/v1/machines/auth`, with no API error code) the client reports error code `agent_access_disabled`. Delete requires matching `mid` and `confirm_mid`. Open defaults to owner/desktop/300 seconds, with explicit share mode and 60..900-second expiry. MCP output uses `structuredContent` and `isError:true` for failures. Exact backend-generated per-operation output schemas remain an integration requirement pending the backend OpenAPI export; the client currently publishes an envelope with public API result objects.

## Agent discovery release matrix

Setup installs versioned skills and hashed entries; run `qsp box doctor` to inspect presence, then verify in each client UI. Presence alone is not discovery evidence.

- Claude Code: `~/.claude/skills`; MCP via installed `claude mcp add --transport stdio --scope user`. [Official MCP docs](https://code.claude.com/docs/en/mcp).
- Codex: `~/.agents/skills`; MCP table `mcp_servers.quicksilver` in `$CODEX_HOME/config.toml` or `~/.codex/config.toml`. [Official MCP docs](https://developers.openai.com/codex/mcp/).
- Cursor: `~/.cursor/mcp.json`, `mcpServers.quicksilver`. [Official docs](https://cursor.com/docs/mcp).
- OpenCode: shared skill destination plus global `opencode.json/jsonc`, `mcp.quicksilver` local command array. JSONC comments/settings preserved. [Official docs](https://opencode.ai/docs/mcp-servers/).
- Gemini: requested compatibility skill destination `~/.gemini/config/skills`; discovery unverified. Manual CLI/MCP instructions only beyond that.
- Antigravity: generic CLI/skill instructions and manual MCP snippet; no guessed configuration files.

Only the SDK's own client/server handshake is tested automatically; actual agent/version discovery remains a release gate.
