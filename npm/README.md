# quicksilverpro (npm launcher)

```bash
npx quicksilverpro setup        # sign in, install the QuickSilver skill + MCP into your agents
npx quicksilverpro box ls       # your cloud dev boxes
```

This package is a thin launcher for the [QuickSilver Pro CLI](https://github.com/machinefi/qspro-cli) (`qsp`), a Python tool. It runs the matching version through [uv](https://docs.astral.sh/uv/) (`uvx`), or pipx if uv is missing.

- One OpenAI-compatible API for frontier and open models: https://quicksilverpro.io
- QuickSilver Machines: cloud dev boxes with Claude Code, Codex, OpenCode and Hermes ready out of the box. Agents ready out of the box. One unified bill. Zero friction.
- Docs: https://quicksilverpro.io/docs/machines/cli/

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

Logs retain a circular 1 MiB per stream (2 MiB total per job). Responses
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


Editor SSH: after setup, run `npx quicksilverpro box ssh-config <machine>` once.
Use a stable installed `qsp` executable for your editor's ProxyCommand (`pipx install quicksilverpro`).
`qsp box ssh <machine>` connects interactively; `ssh -L` forwarding works. Connecting a
paused box resumes billing. The bundled MCP `box_ssh_info` tool returns local setup instructions.
