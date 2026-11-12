# Machines beta client contract (0.6.0)

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

## Data-plane contract

- Token request: `{purpose:"upload"|"download"|"terminal",resource:...}`. File resources are normalized paths relative to `/home/user`. Upload additionally sends `size`, `overwrite`, and lowercase `sha256`. Download uses the default size 0. No `client_mode`, `path`, or `max_bytes` request fields.
- Response: `{host,token,resource,expires_in,mode:"cli"}` for file grants. Build `${host}/files/${mid}/upload|download?path=${encodeURIComponent(resource)}&token=${encodeURIComponent(token)}`. No account authorization is forwarded; redirects are refused.
- Upload: streaming multipart POST, one field `file`, filename matching the resource basename. Success returns `{path,size,sha256}`; verify exact bytes and checksum. Download: GET; verify `Content-Length` and `X-Checksum-SHA256` against the received bytes. The token response does not contain download size/checksum. Local downloads use a temporary file and atomic commit with explicit overwrite. Limits: 64 MiB CLI (oversized upload rejected before requesting a token), 256 KiB MCP, 64 KiB chunks, 10-second I/O timeout and 300-second transfer deadline.
- Exec CLI accepts `MID -- cmd args...` with literal argv, exclusively with respect to `--argv-json` / `--command`. CLI options precede `--`. Only an explicit HTTP 409 `Machine is not ready.` or `Guest runner is not ready.` response before execution is retried, with bounded backoff up to 30 seconds. Generic 409 responses, transport errors, and failures after the stream starts are never retried. The backend at origin/main may replace host readiness errors with `Guest execution could not start. Check status before retrying.`; that ambiguous error is deliberately not retried.
- Server errors use `detail`, `error.message`, or the manager's `message` in both human and JSON output, with credential/capability redaction. `qsp box ls` displays id/name/state/size/hours this month and an account summary; `--json` retains the API result.
- Terminal accepts binary stdin and returns binary or text stdout. Resize uses JSON `{type:"resize",cols,rows}`. Ctrl-C is forwarded as raw byte 0x03; Ctrl-] disconnects. Terminal state and signal handlers are restored on all exit paths. Shell is POSIX-only, with optional `shell` extra.

The terminal adapter builds `wss://${host}/terminal/${mid}?token=...`; interactive terminal compatibility requires a separate TTY smoke test.

## Local MCP

Install `quicksilverpro[mcp]==0.6.0` with Python >=3.10; SDK pinned to `mcp==1.25.0` (SDK negotiates supported protocol versions). `qsp mcp` is stdio only and resolves credentials itself. No secrets in agent config. `qsp mcp --check` verifies the installation without starting stdio. Protocol cancellation closes the active API/file transports; backend disconnect handling must cancel the guest job tree. Tools: `box_create/list/get/wait/exec/files_list/upload/download/pause/resume/delete/open`.

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

## Detached jobs


For interactive commands, request `Accept: text/event-stream` on
`POST /v1/machines/{mid}/exec`. Streams include `: keepalive` comments every
15 seconds, including while commands are quiet. Ignore comment frames. Exec
still has a 300-second total lifetime and stops its entire process group when
the client disconnects. Buffered requests with `timeout_seconds > 90` are
rejected before execution; use streaming or jobs. The CLI and MCP client
select streaming automatically for timeouts over 60 seconds.

For work that must survive a disconnected client, use detached jobs:

```sh
qsp box job start MACHINE_ID --command 'pytest > results.txt' --timeout 86400
qsp box job ls MACHINE_ID
qsp box job status MACHINE_ID JOB_ID
qsp box job logs MACHINE_ID JOB_ID --tail 4096
qsp box job logs MACHINE_ID JOB_ID -f --stream stderr
qsp box job kill MACHINE_ID JOB_ID
```

`start` also accepts argv after `--`, `--argv-json`, `--cwd`, and repeatable
`--env KEY=VALUE`. Commands run as `user`, inside `/home/user`, with the same
environment restrictions as exec. Shell syntax requires explicit `command`.
Do not use `nohup` or `&` inside exec to detach work.

| Method | Path | Scope | Input/result |
| --- | --- | --- | --- |
| POST | `/v1/machines/{mid}/jobs` | `machines:exec` | Exec body; `timeout_seconds` defaults to 86400, max 259200; returns job status |
| GET | `/v1/machines/{mid}/jobs` | `machines:read` | `{jobs: [...]}` |
| GET | `/v1/machines/{mid}/jobs/{jid}` | `machines:read` | Status |
| GET | `/v1/machines/{mid}/jobs/{jid}/logs` | `machines:read` | `stream=stdout|stderr`, `offset`, optional `tail` (0–65536 bytes), `follow=true` for SSE |
| POST | `/v1/machines/{mid}/jobs/{jid}/kill` | `machines:exec` | Stops the job and its descendants |

Dashboard sessions and scoped Machines keys use the existing ownership, machine
restriction and human-control gates. Job operations do not create or resume a
machine. Status contains `id`, `state` (`running`, `exited`, `killed`, `lost`),
`exit_code`, Unix `started_at`/`ended_at`, and `max_runtime_seconds`. A killed or
lost job may have no exit code. Starts are not idempotent: after an unknown
outcome, list jobs before deciding whether to start again.

Logs retain a circular 25 MiB per stream (50 MiB total per job). Responses
contain base64 `data` and absolute byte cursors `offset`, `next_offset`,
`start_offset`, `end_offset`. Each read returns at most 64 KiB. If a cursor is
older than retained data, `offset` advances to `start_offset`; clients can detect
the gap. Poll `next_offset`, or follow SSE `logs` events with keepalive comments.
Disconnecting log follow never kills a detached job. Clients may reconnect using
the last cursor when authority refresh closes a follow stream. Logs are untrusted
command output. Keep durable artifacts in the workspace. At most eight jobs run
at once; history retains the latest 256 jobs, evicting the oldest completed job
on the next start.

Running jobs count as activity even while quiet, preventing idle auto-pause.
Billing enforcement, the machine session runtime cap, and explicit owner pause
still apply. Memory pause freezes jobs; restoring that memory within its
24-hour retention resumes them. The guest service runtime limit uses the guest
clock; it does not imply a 24-hour uninterrupted billable machine session.
Cold restarts, Sleeping, expired memory checkpoints and named snapshot restores
lose processes: old running records become `lost` when inspected. Paused status
is the manager's last observed status; logs require a running machine.

Retryable pre-exec failures include `exec_concurrency_limit` (429),
`runner_not_ready` (409), and `exec_slot_race` (409). Check `retry_after` and the
`Retry-After` header. `exec_outcome_unknown` and `job_outcome_unknown` must never
be retried automatically. MCP equivalents are `box_job_start`, `box_job_list`,
`box_job_status`, `box_job_logs` (bounded polling), and `box_job_kill`.
