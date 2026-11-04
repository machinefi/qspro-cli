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
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx

from . import __version__

MAX_FILE = 64 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024


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
            "auth_required", "Run qsp setup or set QSP_MACHINES_KEY (a qsm_ Machines key).", 2
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
            config_dir() / "machines.json", json.dumps({"machines": {"key": key}}), private=True
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


def exec_body(argv=None, command=None, cwd=None, timeout_seconds=30, env=None):
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
    if not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 300:
        raise MachineError("invalid_exec", "Exec timeout must be 1..300 seconds.", 2)
    body = {"timeout_seconds": timeout_seconds}
    body["argv" if argv is not None else "command"] = argv if argv is not None else command
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
        origin = origin or os.environ.get("QSP_MACHINES_URL", "https://api.quicksilverpro.io")
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
            # Do not echo response bodies, URLs, headers or exception text: they may contain capabilities.
            try:
                raw = response.json().get("error", {}) if response.is_stream_consumed else {}
                raw = raw if isinstance(raw, dict) else {}
            except (ValueError, AttributeError):
                raw = {}
            code = raw.get("code", "http_" + str(response.status_code))
            code = (
                code
                if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", code)
                else "api_error"
            )
            raise MachineError(
                code,
                "Machines API request failed (HTTP %s)." % response.status_code,
                2 if response.status_code in (401, 403) else 1,
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
                "invalid_request_id", "Request ID must be 16..64 letters, digits or hyphens.", 2
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
                    "wait_timeout", "Wait deadline exceeded; machine was left unchanged.", 124
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

    def execute(self, mid, **kwargs):
        body = exec_body(**kwargs)
        try:
            result = self.request(
                "POST",
                "/v1/machines/" + mid_value(mid) + "/exec",
                json=body,
                timeout=body["timeout_seconds"] + 10,
            )
        except MachineError as error:
            if error.code in ("transport_error", "invalid_response"):
                raise MachineError(
                    "outcome_unknown", "Execution outcome unknown; do not automatically retry."
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
                "outcome_unknown", "No terminal execution evidence; do not automatically retry."
            )
        return result

    def events(self, mid, **kwargs):
        self.ensure_active()
        body = exec_body(**kwargs)
        terminal, sequence, total = False, -1, 0
        try:
            with self.http.stream(
                "POST",
                "/v1/machines/" + mid_value(mid) + "/exec",
                json=body,
                headers={"Accept": "text/event-stream"},
                timeout=body["timeout_seconds"] + 10,
            ) as response:
                self.check(response)
                event, chunks, frame_size = "message", [], 0
                for line in response.iter_lines():
                    frame_size += len(line.encode())
                    if frame_size > MAX_OUTPUT + 65536:
                        raise MachineError(
                            "output_limit", "Execution stream frame exceeds output limit."
                        )
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
                    "outcome_unknown", "Execution stream ended without exit evidence; do not retry."
                )
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise MachineError(
                "outcome_unknown", "Execution stream interrupted or malformed; do not retry."
            )

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
            json={"purpose": purpose, "client_mode": "cli", **fields},
        )
        url = grant.get("url", "")
        parsed = urlsplit(url)
        if (
            parsed.scheme != ("wss" if purpose == "terminal" else "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise MachineError("invalid_grant", "API returned an invalid data-plane capability.")
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
                raise MachineError("file_too_large", "File exceeds transfer limit.", 2)
            digest.update(chunk)
        source.seek(0)
        checksum = digest.hexdigest()
        grant = self.grant(
            mid, "upload", path=path, max_bytes=size, sha256=checksum, overwrite=overwrite
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
        grant = self.grant(mid, "download", path=path, max_bytes=limit)
        if type(grant.get("size")) is not int or not isinstance(grant.get("sha256"), str):
            raise MachineError("invalid_grant", "Download grant requires byte count and checksum.")
        if grant["size"] > limit:
            raise MachineError("transfer_limit", "Download exceeds byte limit.")
        size, digest, started = 0, hashlib.sha256(), time.monotonic()
        try:
            with self.data_client() as data:
                with data.stream("GET", grant["url"]) as response:
                    self.check(response)
                    for chunk in response.iter_bytes(65536):
                        size += len(chunk)
                        if size > limit or time.monotonic() - started > 300:
                            raise MachineError(
                                "transfer_limit", "Download exceeded byte limit or deadline."
                            )
                        digest.update(chunk)
                        output.write(chunk)
            if grant.get("size") != size or grant.get("sha256") != digest.hexdigest():
                raise MachineError(
                    "checksum_mismatch", "Download byte count/checksum was not verified."
                )
            return {"path": path, "size": size, "sha256": digest.hexdigest()}
        except httpx.HTTPError:
            raise MachineError(
                "transfer_error", "Download failed; partial destination was discarded."
            )
