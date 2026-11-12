"""Machines-only public API client. No inference credentials or automatic mutations."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import signal
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, ExitStack
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlencode

import httpx

from . import __version__

MAX_FILE = 64 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024


AGENT_ACCESS_DISABLED = "agent_access_disabled"
AGENT_ACCESS_MESSAGE = (
    "QuickSilver Machines agent access isn't enabled for this account yet. "
    "Check that your qsm_ key is current, or contact QuickSilver support."
)


class MachineError(Exception):
    def __init__(self, code, message, exit_code=1, **details):
        super().__init__(message)
        self.code, self.message, self.exit_code = code, message, exit_code
        self.details = details

    def payload(self):
        return {"code": self.code, "message": self.message, **self.details}


def config_dir():
    return Path(os.environ.get("QSP_CONFIG_DIR", Path.home() / ".config/quicksilverpro"))


def safe_path(path):
    path = Path(path).absolute()
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise MachineError("unsafe_path", "Refusing a symlink in a managed path.", 2)
    return path


def atomic_write(path, data, private=False, expected=Ellipsis):
    """Replace atomically; optional expected bytes (None for absent) detect stale writes."""
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if private:
        os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".qsp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data if isinstance(data, bytes) else data.encode())
            out.flush()
            os.fsync(out.fileno())
        os.chmod(temporary, 0o600)
        safe_path(path)
        if expected is not Ellipsis:
            current = path.read_bytes() if path.exists() else None
            if current != expected:
                raise MachineError(
                    "config_conflict",
                    "Configuration changed during setup; preserving it. Rerun setup.",
                    2,
                )
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, default=None):
    path = safe_path(path)
    if not path.exists():
        return {} if default is None else default
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, OSError):
        raise MachineError("invalid_config", "Cannot read managed JSON configuration.", 2)


def resolve_key():
    key = os.environ.get("QSP_MACHINES_KEY")
    if not key:
        try:
            import keyring

            key = keyring.get_password("quicksilverpro-machines", "machines")
        except Exception:
            pass
    if not key:
        entry = read_json(config_dir() / "machines.json").get("machines", {})
        key = entry.get("key") if isinstance(entry, dict) else None
    if not isinstance(key, str) or not key.strip().startswith("qsm_"):
        raise MachineError(
            "auth_required",
            "Run qsp setup or set QSP_MACHINES_KEY (a qsm_ Machines key).",
            2,
        )
    return key.strip()


def store_key(key):
    if not key.startswith("qsm_"):
        raise MachineError("invalid_key", "A separate qsm_ Machines key is required.", 2)
    try:
        import keyring

        keyring.set_password("quicksilverpro-machines", "machines", key)
        return
    except Exception:
        atomic_write(
            config_dir() / "machines.json",
            json.dumps({"machines": {"key": key}}),
            private=True,
        )


def mid_value(mid):
    if not re.fullmatch(r"[a-f0-9]{12}", mid):
        raise MachineError(
            "invalid_mid", "Machine ID must be 12 lowercase hexadecimal characters.", 2
        )
    return mid


def workspace_path(path):
    if not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path:
        raise MachineError("invalid_path", "Invalid workspace path.", 2)
    if path.startswith("/home/user/"):
        path = path[len("/home/user/") :]
    elif path == "/home/user":
        path = "."
    if (
        path.startswith("/")
        or ".." in path.split("/")
        or any(len(p.encode()) > 255 for p in path.split("/"))
    ):
        raise MachineError("invalid_path", "Paths must stay inside /home/user.", 2)
    return str(PurePosixPath(path))


def exec_body(argv=None, command=None, cwd=None, timeout_seconds=30, env=None, _max_timeout=300):
    if (argv is None) == (command is None):
        raise MachineError("invalid_exec", "Provide exactly one of argv or command.", 2)
    if argv is not None and (
        not isinstance(argv, list)
        or not 1 <= len(argv) <= 256
        or any(not isinstance(a, str) or "\x00" in a for a in argv)
    ):
        raise MachineError("invalid_exec", "argv must contain 1..256 strings without NUL.", 2)
    if command is not None and (
        not isinstance(command, str) or not 1 <= len(command) <= 32768 or "\x00" in command
    ):
        raise MachineError(
            "invalid_exec", "command must contain 1..32768 characters without NUL.", 2
        )
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= _max_timeout:
        raise MachineError("invalid_exec", "Runtime must be 1..%s seconds." % _max_timeout, 2)
    body = {"timeout_seconds": timeout_seconds}
    body["argv" if argv is not None else "command"] = (
        argv if argv is not None else command
    )
    if cwd is not None:
        body["cwd"] = "/home/user/" + workspace_path(cwd)
    if env is not None:
        body["env"] = env  # The server enforces the restricted environment allowlist.
    if len(json.dumps(body).encode()) > 65536:
        raise MachineError("body_too_large", "Control body exceeds 64 KiB.", 2)
    return body


def exec_exit(result):
    if result.get("truncated"):
        return 1
    if result.get("timed_out"):
        return 124
    sig = result.get("signal")
    if sig:
        try:
            number = (
                int(sig)
                if str(sig).isdigit()
                else int(
                    getattr(signal, str(sig) if str(sig).startswith("SIG") else "SIG" + str(sig))
                )
            )
            return min(255, 128 + number)
        except (ValueError, AttributeError):
            return 1
    code = result.get("exit_code")
    return code if type(code) is int and 0 <= code <= 255 else 1


class MachinesClient:
    def __init__(self, key=None, origin=None, transport=None):
        self.key = key or resolve_key()
        if not isinstance(self.key, str) or not self.key.startswith("qsm_"):
            raise MachineError("invalid_key", "A separate qsm_ Machines key is required.", 2)
        origin = origin or os.environ.get("QSP_MACHINES_URL", "https://pay.quicksilverpro.io")
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise MachineError(
                "invalid_origin",
                "QSP_MACHINES_URL must be an HTTPS origin without a path or credentials.",
                2,
            )
        self.origin = origin.rstrip("/")
        self._lock = threading.Lock()
        self._cancelled = False
        self._data_clients = set()
        self.http = httpx.Client(
            base_url=self.origin,
            headers={
                "Authorization": "Bearer " + self.key,
                "User-Agent": "quicksilverpro-cli/" + __version__,
            },
            follow_redirects=False,
            timeout=30,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cancel()

    def cancel(self):
        """Close active transports so the server observes disconnect/cancellation."""
        with self._lock:
            self._cancelled = True
            clients = [self.http, *self._data_clients]
        for client in clients:
            client.close()

    def ensure_active(self):
        with self._lock:
            if self._cancelled:
                raise MachineError(
                    "interrupted",
                    "Machines operation was cancelled; do not retry unknown outcomes.",
                    130,
                )

    @contextmanager
    def data_client(self):
        with httpx.Client(follow_redirects=False, timeout=10) as client:
            with self._lock:
                if self._cancelled:
                    raise MachineError("interrupted", "Transfer was cancelled.", 130)
                self._data_clients.add(client)
            try:
                yield client
            finally:
                with self._lock:
                    self._data_clients.discard(client)

    def check(self, response):
        if not 200 <= response.status_code < 300:
            # Read JSON errors even on streaming responses; never echo raw bodies/URLs.
            try:
                response.read()
                data = response.json()
                raw = data.get("error", {})
                raw = raw if isinstance(raw, dict) else {}
            except (ValueError, AttributeError):
                raw, data = {}, {}
            status = response.status_code
            has_code = isinstance(raw.get("code"), str)
            code = raw.get("code", "http_" + str(status))
            code = (
                code
                if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", code)
                else "api_error"
            )
            detail = data.get("detail")
            message = (
                raw.get("message")
                or (detail.get("message") if isinstance(detail, dict) else detail)
                or data.get("message")
            )
            if not isinstance(message, str) or not message.strip():
                message = "Machines API request failed (HTTP %s)." % status
            # Preserve public diagnostics while redacting credentials and capability URLs.
            message = message.replace(self.key, "[redacted]")
            message = re.sub(
                r"qsm_[A-Za-z0-9_-]+|https?://[^\s]+|wss?://[^\s]+",
                "[redacted]",
                message,
            )
            message = re.sub(r"(?i)token=[^\s&]+", "token=[redacted]", message)
            exit_code = 2 if status in (401, 403) else 1
            # Agent access disabled: 401 anywhere, or 404 on the account-level endpoints
            # (a 404 on a specific machine ID still means "no such machine").
            account_level = response.request.url.path.rstrip("/") in (
                "/v1/machines",
                "/v1/machines/auth",
            )
            if not has_code and (status == 401 or (status == 404 and account_level)):
                code = AGENT_ACCESS_DISABLED
                message = AGENT_ACCESS_MESSAGE + " " + message
                exit_code = 2
            raise MachineError(
                code,
                message,
                exit_code,
                status_code=response.status_code,
                retryable=raw.get("retryable") is True,
                retry_after_seconds=raw.get("retry_after_seconds")
                if isinstance(raw.get("retry_after_seconds"), (int, float))
                else None,
            )

    def request(self, method, path, **kwargs):
        self.ensure_active()
        try:
            response = self.http.request(method, path, **kwargs)
            self.check(response)
            if response.status_code == 204:
                return {}
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except httpx.HTTPError:
            raise MachineError(
                "transport_error", "Machines request failed; no mutation was retried."
            )
        except ValueError:
            raise MachineError("invalid_response", "Machines API returned invalid JSON.")

    def auth(self):
        return self.request("GET", "/v1/machines/auth")

    def list(self):
        return self.request("GET", "/v1/machines")

    def get(self, mid):
        return self.request("GET", "/v1/machines/" + mid_value(mid))

    def clone(self, mid, from_snapshot=None, name=None, count=1, request_id=None, on_request_id=None):
        if not isinstance(mid, str) or not mid:
            raise MachineError("invalid_machine", "Choose a source machine ID or name.", 2)
        if not re.fullmatch(r"[a-f0-9]{12}", mid):
            matches = [m for m in self.list().get("machines", []) if m.get("name") == mid]
            if len(matches) != 1:
                raise MachineError("invalid_machine", "Source name is missing or ambiguous; use its machine ID.", 2)
            mid = mid_value(matches[0]["id"])
        if type(count) is not int or not 1 <= count <= 64:
            raise MachineError("invalid_count", "Count must be 1..64 and within your account limit.", 2)
        if name is not None and (not isinstance(name, str) or not 1 <= len(name.strip()) <= 64):
            raise MachineError("invalid_name", "Name must be 1..64 characters.", 2)
        if from_snapshot is not None and (not isinstance(from_snapshot, str) or not 1 <= len(from_snapshot) <= 80):
            raise MachineError("invalid_snapshot", "Snapshot must be an ID or name up to 80 characters.", 2)
        body = {"count": count, "name": name, "from_snapshot": from_snapshot}
        path = config_dir() / "clone-requests.json"
        journal = read_json(path)
        fingerprint = hashlib.sha256((self.origin + hashlib.sha256(self.key.encode()).hexdigest() + mid + json.dumps(body, sort_keys=True)).encode()).hexdigest()
        request_id = request_id or journal.get(fingerprint, {}).get("request_id") or str(uuid.uuid4())
        if not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", request_id):
            raise MachineError("invalid_request_id", "Request ID must be 16..64 letters, digits or hyphens.", 2)
        atomic_write(path, json.dumps({**journal, fingerprint: {"request_id": request_id}}), private=True)
        if on_request_id:on_request_id(request_id)
        try:
            result = self.request("POST", "/v1/machines/" + mid + "/clone", json={**body, "request_id": request_id})
        except MachineError as error:
            error.details["request_id"] = request_id
            raise
        return {**result, "request_id": request_id}

    def create(self, name, size=2, request_id=None, on_request_id=None):
        if not isinstance(name, str) or not 1 <= len(name) <= 64 or size not in (2, 4):
            raise MachineError(
                "invalid_create", "Name must be 1..64 characters and size 2 or 4.", 2
            )
        body = {"name": name, "size": size}
        # Persist intent BEFORE the request; same parameters recover a lost response across invocations.
        path = config_dir() / "create-requests.json"
        journal = read_json(path)
        fingerprint = hashlib.sha256(
            (
                self.origin
                + hashlib.sha256(self.key.encode()).hexdigest()
                + json.dumps(body, sort_keys=True)
            ).encode()
        ).hexdigest()
        request_id = (
            request_id or journal.get(fingerprint, {}).get("request_id") or str(uuid.uuid4())
        )
        if not re.fullmatch(r"[a-zA-Z0-9-]{16,64}", request_id):
            raise MachineError(
                "invalid_request_id",
                "Request ID must be 16..64 letters, digits or hyphens.",
                2,
            )
        journal[fingerprint] = {"request_id": request_id, **body}
        atomic_write(path, json.dumps(journal), private=True)
        if on_request_id:
            on_request_id(request_id)
        try:
            result = self.request("POST", "/v1/machines", json={**body, "request_id": request_id})
        except MachineError as error:
            error.details["request_id"] = request_id
            raise
        return {**result, "request_id": request_id}

    def action(self, mid, action):
        if action not in ("pause", "resume", "delete"):
            raise MachineError("invalid_action", "Unsupported machine action.", 2)
        return self.request("POST", "/v1/machines/%s/action/%s" % (mid_value(mid), action), json={})

    def wait(self, mid, timeout=180, deleted=False):
        deadline, delay = time.monotonic() + timeout, 1.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MachineError(
                    "wait_timeout",
                    "Wait deadline exceeded; machine was left unchanged.",
                    124,
                )
            try:
                data = self.request(
                    "GET", "/v1/machines/" + mid_value(mid), timeout=min(30, remaining)
                )
            except MachineError as error:
                if deleted and error.details.get("status_code") == 404:
                    return {"id": mid, "state": "Deleted"}
                if error.details.get("status_code") not in (429, 503):
                    raise
                retry_after = error.details.get("retry_after_seconds")
                if isinstance(retry_after, (int, float)):
                    time.sleep(min(max(0, deadline - time.monotonic()), max(0, retry_after)))
                data = {}
            machine = data.get("machine", data)
            state = machine.get("state", machine.get("status"))
            if (deleted and state == "Deleted") or (
                not deleted and state == "Running" and machine.get("guest_ready") is True
            ):
                return data
            if not deleted and state in ("Failed", "Deleted", "Paused", "Stopped"):
                raise MachineError(
                    "not_running", "Machine cannot become ready in its current state."
                )
            time.sleep(min(max(0, deadline - time.monotonic()), delay * random.uniform(0.8, 1.2)))
            delay = min(5, delay * 1.5)

    @contextmanager
    def exec_response(self, mid, body, stream=False):
        """Retry only an explicit readiness rejection, before handing off any response."""
        deadline, delay = time.monotonic() + 30, 1
        for attempt in range(10):
            self.ensure_active()
            stack = ExitStack()
            try:
                response = stack.enter_context(
                    self.http.stream(
                        "POST",
                        "/v1/machines/" + mid_value(mid) + "/exec",
                        json=body,
                        headers={"Accept": "text/event-stream"} if stream else {},
                        timeout=body["timeout_seconds"] + 10,
                    )
                )
                self.check(response)
            except MachineError as error:
                stack.close()
                # Exact public pre-execution rejection, never a generic conflict or stream error.
                remaining = deadline - time.monotonic()
                if (
                    error.details.get("status_code") not in (409, 429)
                    or error.code in ("outcome_unknown", "exec_outcome_unknown")
                    or (error.code not in ('runner_not_ready', 'exec_slot_race', 'exec_concurrency_limit') and error.message
                    not in ("Machine is not ready.", "Guest runner is not ready."))
                    or remaining <= 0
                    or attempt == 9
                ):
                    raise
                time.sleep(min(delay, remaining))
                delay = min(5, delay * 2)
                continue
            except BaseException:
                stack.close()
                raise
            with stack:
                yield response
            return

    def execute(self, mid, **kwargs):
        body = exec_body(**kwargs)
        if body['timeout_seconds'] > 60:
            result = None
            output = {'stdout': [], 'stderr': []}
            for event in self.events(mid, **kwargs):
                if event['event'] in output:output[event['event']].append(event['data'])
                elif event['event'] == 'exit':result = event
            if result is None:raise MachineError('outcome_unknown', 'Missing execution exit evidence.')
            return {**{k:v for k,v in result.items() if k not in ('event','seq')}, **{k:''.join(v) for k,v in output.items()}}
        try:
            with self.exec_response(mid, body) as response:
                response.read()
                result = response.json()
                if not isinstance(result, dict):
                    raise ValueError()
        except (httpx.HTTPError, ValueError):
            raise MachineError(
                "outcome_unknown",
                "Execution outcome unknown; do not automatically retry.",
            )
        except MachineError as error:
            if error.code in ("transport_error", "invalid_response"):
                raise MachineError(
                    "outcome_unknown",
                    "Execution outcome unknown; do not automatically retry.",
                )
            raise
        if (
            sum(
                len(result.get(key, "").encode())
                for key in ("stdout", "stderr")
                if isinstance(result.get(key, ""), str)
            )
            > MAX_OUTPUT
        ):
            raise MachineError("output_limit", "Execution output exceeds 1 MiB.")
        if (
            type(result.get("exit_code")) is not int
            and not result.get("signal")
            and not result.get("timed_out")
            and not result.get("truncated")
        ):
            raise MachineError(
                "outcome_unknown",
                "No terminal execution evidence; do not automatically retry.",
            )
        return result

    def events(self, mid, **kwargs):
        self.ensure_active()
        body = exec_body(**kwargs)
        terminal, sequence, total = False, -1, 0
        try:
            with self.exec_response(mid, body, stream=True) as response:
                event, chunks, frame_size = "message", [], 0
                for line in response.iter_lines():
                    frame_size += len(line.encode())
                    if frame_size > MAX_OUTPUT + 65536:
                        raise MachineError(
                            "output_limit",
                            "Execution stream frame exceeds output limit.",
                        )
                    if not line and not chunks:
                        event, frame_size = 'message', 0
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        chunks.append(line[5:].lstrip(" "))
                    elif not line and chunks:
                        data = json.loads("\n".join(chunks))
                        if not isinstance(data, dict):
                            raise ValueError()
                        if event in ("stdout", "stderr"):
                            seq = data["seq"]
                            if (
                                type(seq) is not int
                                or seq <= sequence
                                or not isinstance(data["data"], str)
                            ):
                                raise ValueError()
                            sequence = seq
                            total += len(data["data"].encode())
                            if total > MAX_OUTPUT:
                                raise MachineError(
                                    "output_limit", "Execution output exceeds 1 MiB."
                                )
                        elif event == "exit":
                            if type(data.get("exit_code")) is not int and not any(
                                data.get(k) for k in ("signal", "timed_out", "truncated")
                            ):
                                raise ValueError()
                            terminal = True
                        elif event == "error":
                            raise MachineError("exec_error", "Execution stream reported a failure.")
                        else:
                            raise ValueError()
                        yield {**data, "event": event}
                        if terminal:
                            return
                        event, chunks, frame_size = "message", [], 0
            if not terminal:
                raise MachineError(
                    "outcome_unknown",
                    "Execution stream ended without exit evidence; do not retry.",
                )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise MachineError(
                "outcome_unknown",
                "Execution stream interrupted or malformed; do not retry.",
            )

    def job_start(self, mid, **kwargs):
        kwargs.setdefault('timeout_seconds', 86400)
        body = exec_body(**kwargs, _max_timeout=259200)
        return self.request('POST', '/v1/machines/' + mid_value(mid) + '/jobs', json=body)

    def job(self, mid, action='list', job_id=None, **options):
        path = '/v1/machines/' + mid_value(mid) + '/jobs'
        if action != 'list':
            if not isinstance(job_id, str) or not re.fullmatch('[a-f0-9]{32}', job_id):
                raise MachineError('invalid_job', 'Invalid job ID.', 2)
            path += '/' + job_id
        if action in ('kill', 'logs'):path += '/' + action
        if action not in ('list', 'status', 'kill', 'logs'):
            raise MachineError('invalid_job', 'Invalid job operation.', 2)
        return self.request('POST' if action == 'kill' else 'GET', path, **({'json':{}} if action == 'kill' else {'params':options} if options else {}))

    def job_follow(self, mid, job_id, **options):
        import base64
        if not re.fullmatch('[a-f0-9]{32}', job_id):raise MachineError('invalid_job', 'Invalid job ID.', 2)
        self.ensure_active()
        path = '/v1/machines/' + mid_value(mid) + '/jobs/' + job_id + '/logs'
        # Reconnect with the absolute cursor: following never starts new work.
        try:
            while True:
                with self.http.stream('GET', path, params={**options, 'follow':'true'}, timeout=35) as response:
                    self.check(response)
                    for line in response.iter_lines():
                        if line.startswith('data: '):
                            if len(line)>131072:raise MachineError('output_limit', 'Invalid job log frame.')
                            event=json.loads(line[6:]);base64.b64decode(event['data'],validate=True)
                            options.pop('tail',None);options['offset']=event['next_offset']
                            yield event
                            if event['state']!='running' and event['next_offset']>=event['end_offset']:return
                self.ensure_active()
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise MachineError('job_logs_interrupted', 'Job log stream interrupted; resume from the last next_offset.', next_offset=options.get('offset',0)) from None

    def files(self, mid, path="."):
        return self.request(
            "GET",
            "/v1/machines/" + mid_value(mid) + "/files",
            params={"path": workspace_path(path)},
        )

    def link(self, mid, view="desktop", mode="owner", expires_in_seconds=300):
        if (
            view not in ("desktop", "terminal")
            or mode not in ("owner", "share")
            or not 60 <= expires_in_seconds <= 900
        ):
            raise MachineError("invalid_link", "Invalid link view, mode or expiry.", 2)
        return self.request(
            "POST",
            "/v1/machines/" + mid_value(mid) + "/links",
            json={"view": view, "mode": mode, "expires_in_seconds": expires_in_seconds},
        )

    def grant(self, mid, purpose, **fields):
        grant = self.request(
            "POST",
            "/v1/machines/" + mid_value(mid) + "/token",
            json={"purpose": purpose, **fields},
        )
        host, token = grant.get("host", ""), grant.get("token")
        parsed = urlsplit(host)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
            or not isinstance(token, str)
            or not token
        ):
            raise MachineError(
                "invalid_grant", "API returned an invalid data-plane capability."
            )
        if purpose in ("upload", "download"):
            resource = grant.get("resource")
            if resource != fields.get("resource"):
                raise MachineError(
                    "invalid_grant", "API returned a different file resource."
                )
            grant["url"] = (
                host.rstrip("/")
                + "/files/"
                + mid
                + "/"
                + purpose
                + "?"
                + urlencode({"path": resource, "token": token})
            )
        elif purpose == "terminal":
            grant["url"] = (
                "wss://"
                + parsed.netloc
                + "/terminal/"
                + mid
                + "?"
                + urlencode({"token": token})
            )
        return grant

    def upload(self, mid, path, source, overwrite=False, limit=MAX_FILE):
        path = workspace_path(path)
        size, digest = 0, hashlib.sha256()
        while True:
            chunk = source.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise MachineError(
                    "file_too_large", "Agent files are limited to 64 MiB.", 2
                )
            digest.update(chunk)
        source.seek(0)
        checksum = digest.hexdigest()
        grant = self.grant(
            mid,
            "upload",
            resource=path,
            size=size,
            sha256=checksum,
            overwrite=overwrite,
        )
        started = time.monotonic()

        class BoundedSource:
            sent = 0

            def read(self, count=65536):
                if time.monotonic() - started > 300:
                    raise MachineError("transfer_limit", "Upload exceeded deadline.")
                chunk = source.read(min(count, 65536) if count >= 0 else 65536)
                self.sent += len(chunk)
                if self.sent > size:
                    raise MachineError("transfer_limit", "Upload changed size.")
                return chunk

            def seek(self, *args):
                return source.seek(*args)

            def tell(self):
                return source.tell()

        bounded = BoundedSource()
        try:
            # A separate client has NO account authorization header. Grant URL is the only authority.
            with self.data_client() as data:
                response = data.post(
                    grant["url"],
                    files={"file": (PurePosixPath(path).name, bounded, "application/octet-stream")},
                )
                self.check(response)
                result = response.json()
            if result.get("size") != size or result.get("sha256") != checksum:
                raise MachineError(
                    "checksum_mismatch", "Upload byte count/checksum was not verified."
                )
            return {"path": path, "size": size, "sha256": checksum}
        except (httpx.HTTPError, ValueError):
            raise MachineError("transfer_error", "Upload failed; no transfer was retried.")

    def download(self, mid, path, output, limit=MAX_FILE):
        path = workspace_path(path)
        grant = self.grant(mid, "download", resource=path)
        size, digest, started = 0, hashlib.sha256(), time.monotonic()
        try:
            with self.data_client() as data:
                with data.stream("GET", grant["url"]) as response:
                    self.check(response)
                    length = response.headers.get("Content-Length", "")
                    checksum = response.headers.get("X-Checksum-SHA256", "")
                    if not length.isdigit() or not re.fullmatch(
                        r"[a-f0-9]{64}", checksum
                    ):
                        raise MachineError(
                            "invalid_response",
                            "Download requires byte count and SHA-256 headers.",
                        )
                    expected_size = int(length)
                    if expected_size > limit:
                        raise MachineError(
                            "transfer_limit",
                            "Download exceeds byte limit (64 MiB CLI; 256 KiB MCP).",
                        )
                    for chunk in response.iter_bytes(65536):
                        size += len(chunk)
                        if size > limit or time.monotonic() - started > 300:
                            raise MachineError(
                                "transfer_limit",
                                "Download exceeded byte limit or deadline.",
                            )
                        digest.update(chunk)
                        output.write(chunk)
            if expected_size != size or checksum != digest.hexdigest():
                raise MachineError(
                    "checksum_mismatch",
                    "Download byte count/checksum was not verified.",
                )
            return {"path": path, "size": size, "sha256": digest.hexdigest()}
        except httpx.HTTPError:
            raise MachineError(
                "transfer_error", "Download failed; partial destination was discarded."
            )
