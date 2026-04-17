"""QuickSilver Pro CLI — single-file implementation.

Design goals:
  1. Drop-in OpenAI-compatible thin wrapper: `qsp chat "..."` works immediately.
  2. AI-agent friendly: every command accepts `--json` and emits structured
     output on stdout with errors on stderr and non-zero exit codes on failure.
  3. No config surprises: API key lives in `~/.config/quicksilverpro/config.json`
     (or `$QSP_CONFIG_DIR`); the `QSP_API_KEY` env var overrides.

The CLI talks to two endpoints:
  - https://api.quicksilverpro.io/v1   — OpenAI-shaped inference
  - https://pay.quicksilverpro.io/v1   — account + usage + keys management
"""

from __future__ import annotations

import json
import os
import sys
import time
import webbrowser
from pathlib import Path

import click
import httpx
from rich.console import Console
from rich.table import Table

from . import __version__

# ────────────────────────── constants ──────────────────────────

DEFAULT_API_URL  = os.environ.get("QSP_API_URL",  "https://api.quicksilverpro.io/v1")
DEFAULT_AUTH_URL = os.environ.get("QSP_AUTH_URL", "https://pay.quicksilverpro.io")
DEFAULT_APP_URL  = os.environ.get("QSP_APP_URL",  "https://quicksilverpro.io")
DEFAULT_MODEL    = os.environ.get("QSP_MODEL",    "deepseek-v3")
HTTP_TIMEOUT     = float(os.environ.get("QSP_HTTP_TIMEOUT", "60"))

CONFIG_DIR = Path(os.environ.get("QSP_CONFIG_DIR",
                                 Path.home() / ".config" / "quicksilverpro"))
CONFIG_PATH = CONFIG_DIR / "config.json"

# stderr for humans, stdout reserved for parseable data when `--json` is used.
_err = Console(stderr=True)
_out = Console()


# ────────────────────────── config + auth ──────────────────────────

def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    try:
        os.chmod(CONFIG_PATH, 0o600)  # key is sensitive — best-effort on POSIX.
    except Exception:
        pass


def _resolve_api_key() -> str | None:
    """QSP_API_KEY env var wins; else fall back to ~/.config store."""
    env = os.environ.get("QSP_API_KEY") or os.environ.get("OPENAI_API_KEY_QSP")
    if env:
        return env.strip()
    return (_load_config().get("api_key") or "").strip() or None


def _require_key() -> str:
    k = _resolve_api_key()
    if not k:
        _err.print(
            "[red]Not signed in.[/red] Run [bold]qsp init[/bold] to get started, "
            "or set [bold]QSP_API_KEY[/bold] in your environment."
        )
        sys.exit(2)
    return k


# ────────────────────────── http helpers ──────────────────────────

def _auth_client(key: str | None = None) -> httpx.Client:
    headers = {"User-Agent": f"quicksilverpro-cli/{__version__}"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return httpx.Client(
        base_url=DEFAULT_AUTH_URL,
        timeout=HTTP_TIMEOUT,
        headers=headers,
        follow_redirects=False,
    )


def _api_client(key: str) -> httpx.Client:
    return httpx.Client(
        base_url=DEFAULT_API_URL,
        timeout=HTTP_TIMEOUT,
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": f"quicksilverpro-cli/{__version__}",
            "Content-Type": "application/json",
        },
        follow_redirects=False,
    )


def _extract_error(resp: httpx.Response) -> str:
    try:
        d = resp.json()
        if isinstance(d, dict):
            err = d.get("error")
            if isinstance(err, dict):
                return err.get("message") or str(err)
            if isinstance(d.get("detail"), str):
                return d["detail"]
            return json.dumps(d)
        return resp.text
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"


def _emit(obj, *, as_json: bool, table_fn=None) -> None:
    """JSON goes to stdout; pretty tables go through Rich to stdout."""
    if as_json:
        click.echo(json.dumps(obj, indent=2, default=str))
        return
    if table_fn:
        table_fn(obj)
    else:
        click.echo(json.dumps(obj, indent=2, default=str))


# ────────────────────────── root command group ──────────────────────────

@click.group(
    help=(
        "QuickSilver Pro — OpenAI-compatible API for top open-source LLMs.\n\n"
        "Docs: https://quicksilverpro.io  ·  Status: https://quicksilverpro.io/status"
    ),
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, prog_name="qsp")
def main() -> None:
    pass


# ────────────────────────── qsp init ──────────────────────────

@main.command(help="Sign in: opens the dashboard in a browser, you paste the API key back.")
@click.option("--key", "apikey", default=None, help="Paste an existing API key instead of opening the browser.")
@click.option("--email", default=None, help="Email for a new account (sends verification link).")
def init(apikey: str | None, email: str | None) -> None:
    # Three paths: explicit --key, new-account via --email, or default browser walk-through.
    if apikey:
        _save_key(apikey)
        return

    if email:
        with _auth_client() as c:
            r = c.post("/v1/register", json={"email": email})
        if r.status_code >= 400:
            _err.print(f"[red]Register failed:[/red] {_extract_error(r)}")
            sys.exit(1)
        data = r.json()
        if data.get("verification_required"):
            _out.print(f"✓ Verification email sent to [bold]{email}[/bold]. Click the magic link, "
                       f"copy the key from the dashboard, then run [bold]qsp init --key sk-...[/bold]")
            return
        # Legacy path (email not configured) — returns key immediately.
        if k := data.get("key"):
            _save_key(k)
            return
        _err.print("[red]Unexpected register response shape.[/red]")
        sys.exit(1)

    # Default walkthrough: open the dashboard and guide copy-paste.
    _out.print("Opening [bold]quicksilverpro.io/dashboard[/bold] — sign up or sign in, copy your API key, then paste below.")
    try:
        webbrowser.open(f"{DEFAULT_APP_URL}/dashboard")
    except Exception:
        pass
    pasted = click.prompt("Paste API key", hide_input=True, default="", show_default=False).strip()
    if not pasted:
        _err.print("[red]No key entered — aborted.[/red]")
        sys.exit(1)
    _save_key(pasted)


def _save_key(apikey: str) -> None:
    if not apikey.startswith("sk-"):
        _err.print("[red]That doesn't look like an API key (must start with sk-).[/red]")
        sys.exit(1)
    # Verify by calling /v1/me — catches typos + dead keys up-front.
    with _auth_client(apikey) as c:
        r = c.get("/v1/me")
    if r.status_code >= 400:
        _err.print(f"[red]Key rejected by server:[/red] {_extract_error(r)}")
        sys.exit(1)
    info = r.json()
    cfg = _load_config()
    cfg["api_key"] = apikey
    cfg["email"] = info.get("email") or cfg.get("email", "")
    _save_config(cfg)
    _out.print(f"✓ Signed in as [bold]{info.get('email') or 'unknown'}[/bold]. Key stored at {CONFIG_PATH}.")


# ────────────────────────── qsp logout / whoami ──────────────────────────

@main.command(help="Forget the locally-stored API key.")
def logout() -> None:
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
    _out.print("✓ Signed out.")


@main.command(help="Show the email the CLI is signed in as.")
@click.option("--json", "as_json", is_flag=True)
def whoami(as_json: bool) -> None:
    key = _require_key()
    with _auth_client(key) as c:
        r = c.get("/v1/me")
    if r.status_code >= 400:
        _err.print(f"[red]{_extract_error(r)}[/red]")
        sys.exit(1)
    info = r.json()
    info["api_base"] = DEFAULT_API_URL
    _emit(info, as_json=as_json, table_fn=_print_whoami)


def _print_whoami(info: dict) -> None:
    _out.print(f"Email:      [bold]{info.get('email','—')}[/bold]")
    _out.print(f"Balance:    [bold]${max(0, (info.get('max_budget') or 0) - (info.get('spend') or 0)):.4f}[/bold]")
    _out.print(f"Total spent: ${info.get('spend') or 0:.4f}")
    _out.print(f"API base:   {info.get('api_base')}")


# ────────────────────────── qsp balance ──────────────────────────

@main.command(help="Show current account balance and spend.")
@click.option("--json", "as_json", is_flag=True)
def balance(as_json: bool) -> None:
    key = _require_key()
    with _auth_client(key) as c:
        r = c.get("/v1/me")
    if r.status_code >= 400:
        _err.print(f"[red]{_extract_error(r)}[/red]")
        sys.exit(1)
    info = r.json()
    bal = max(0, (info.get("max_budget") or 0) - (info.get("spend") or 0))
    payload = {
        "balance":    round(bal, 6),
        "loaded":     info.get("max_budget") or 0,
        "spent":      info.get("spend") or 0,
        "currency":   "USD",
    }
    _emit(payload, as_json=as_json, table_fn=lambda p:
          _out.print(f"Balance: [bold]${p['balance']:.4f}[/bold] of ${p['loaded']:.2f} loaded · spent ${p['spent']:.4f}"))


# ────────────────────────── qsp models ──────────────────────────

# Hardcoded so `qsp models` works before the user has signed in. Keep in sync
# with the backend; tests in CI should catch drift.
_MODELS_FALLBACK: list[dict] = [
    {"id": "deepseek-v3", "object": "model", "owned_by": "quicksilverpro",
     "context_length": 131072,
     "pricing": {"prompt": "0.0000002400", "completion": "0.0000007000"},
     "best_for": "chat, coding, structured output"},
    {"id": "deepseek-r1", "object": "model", "owned_by": "quicksilverpro",
     "context_length": 131072,
     "pricing": {"prompt": "0.0000004000", "completion": "0.0000017000"},
     "best_for": "math, multi-step reasoning, logic"},
    {"id": "qwen3.5-35b", "object": "model", "owned_by": "quicksilverpro",
     "context_length": 262144,
     "pricing": {"prompt": "0.0000001300", "completion": "0.0000010000"},
     "best_for": "long-context RAG, summarization (thinking model)"},
]


@main.command(help="List available models.")
@click.option("--json", "as_json", is_flag=True)
def models(as_json: bool) -> None:
    # Try the live endpoint for freshness; fall back to our shipped list if
    # the user isn't signed in, so `qsp models` is useful before `qsp init`.
    key = _resolve_api_key()
    data: list[dict] = []
    if key:
        try:
            with httpx.Client(
                base_url=DEFAULT_API_URL,
                timeout=HTTP_TIMEOUT,
                headers={"Authorization": f"Bearer {key}"},
            ) as c:
                r = c.get("/models")
            if r.status_code < 400:
                data = r.json().get("data", [])
        except httpx.HTTPError:
            data = []
    if not data:
        data = _MODELS_FALLBACK
    _emit(data, as_json=as_json, table_fn=_print_models)


def _print_models(rows: list[dict]) -> None:
    t = Table(show_lines=False)
    t.add_column("id", style="bold")
    t.add_column("context", justify="right")
    t.add_column("prompt $/M", justify="right")
    t.add_column("completion $/M", justify="right")
    for m in rows:
        p = m.get("pricing") or {}
        ctx = m.get("context_length")
        t.add_row(
            m.get("id", ""),
            f"{ctx:,}" if ctx else "—",
            f"{float(p.get('prompt', 0)) * 1_000_000:.2f}" if p else "—",
            f"{float(p.get('completion', 0)) * 1_000_000:.2f}" if p else "—",
        )
    _out.print(t)


# ────────────────────────── qsp chat ──────────────────────────

@main.command(help="One-shot chat completion. Echoes to stdout.")
@click.argument("prompt")
@click.option("-m", "--model", default=DEFAULT_MODEL, show_default=True)
@click.option("-s", "--system", default=None, help="Optional system prompt.")
@click.option("--max-tokens", type=int, default=None)
@click.option("--temperature", type=float, default=None)
@click.option("--stream/--no-stream", default=True, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON response (implies --no-stream).")
def chat(prompt: str, model: str, system: str | None, max_tokens: int | None,
         temperature: float | None, stream: bool, as_json: bool) -> None:
    key = _require_key()
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = {"model": model, "messages": messages}
    if max_tokens is not None:   body["max_tokens"]   = max_tokens
    if temperature is not None:  body["temperature"] = temperature

    # --json means we want the full structured response, so force non-streaming.
    if as_json:
        stream = False

    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        _chat_stream(key, body)
    else:
        _chat_sync(key, body, as_json=as_json)


def _chat_sync(key: str, body: dict, as_json: bool) -> None:
    with _api_client(key) as c:
        r = c.post("/chat/completions", json=body)
    if r.status_code >= 400:
        _err.print(f"[red]{_extract_error(r)}[/red]")
        sys.exit(1)
    data = r.json()
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    msg = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    click.echo(msg)
    usage = data.get("usage") or {}
    cost = usage.get("cost")
    if cost is not None:
        _err.print(
            f"[dim]{usage.get('prompt_tokens',0)} in + "
            f"{usage.get('completion_tokens',0)} out · "
            f"${cost:.6f}[/dim]"
        )


def _chat_stream(key: str, body: dict) -> None:
    last_usage = {}
    exit_code: int | None = None
    bad_status: int | None = None
    bad_body: str | None = None
    wrote_any = False
    try:
        # Nest the client context so the pool is always closed, even on
        # exceptions, KeyboardInterrupt, or sys.exit. Deferring sys.exit until
        # after the client has closed avoids leaking the underlying socket.
        with _api_client(key) as c:
            with c.stream("POST", "/chat/completions", json=body) as r:
                if r.status_code >= 400:
                    # Read body synchronously so _extract_error can parse it.
                    bad_status = r.status_code
                    try:
                        r.read()
                        bad_body = _extract_error(r)
                    except Exception:
                        bad_body = f"HTTP {r.status_code}"
                    exit_code = 1
                else:
                    for line in r.iter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if not payload or payload == "[DONE]":
                            continue
                        try:
                            ev = json.loads(payload)
                        except Exception:
                            continue
                        for ch in ev.get("choices") or []:
                            delta = (ch.get("delta") or {})
                            if delta.get("content"):
                                sys.stdout.write(delta["content"])
                                sys.stdout.flush()
                                wrote_any = True
                        if ev.get("usage"):
                            last_usage = ev["usage"]
    except KeyboardInterrupt:
        # User ctrl-c'd mid-stream — close gracefully, newline so the prompt
        # returns on a fresh line, and signal interrupted via exit code.
        if wrote_any:
            sys.stdout.write("\n")
            sys.stdout.flush()
        _err.print("[yellow]Interrupted.[/yellow]")
        sys.exit(130)
    except httpx.HTTPError as e:
        _err.print(f"\n[red]Network error:[/red] {e}")
        sys.exit(1)

    if exit_code is not None:
        _err.print(f"[red]{bad_body or 'request failed'}[/red]")
        sys.exit(exit_code)

    if wrote_any:
        sys.stdout.write("\n")
        sys.stdout.flush()
    if last_usage.get("cost") is not None:
        _err.print(
            f"[dim]{last_usage.get('prompt_tokens',0)} in + "
            f"{last_usage.get('completion_tokens',0)} out · "
            f"${last_usage['cost']:.6f}[/dim]"
        )


# ────────────────────────── qsp keys ──────────────────────────

@main.group(help="Manage API keys.")
def keys() -> None: pass


@keys.command("list", help="List all of your API keys.")
@click.option("--json", "as_json", is_flag=True)
def keys_list(as_json: bool) -> None:
    key = _require_key()
    with _auth_client(key) as c:
        r = c.get("/v1/keys")
    if r.status_code >= 400:
        _err.print(f"[red]{_extract_error(r)}[/red]")
        sys.exit(1)
    rows = r.json().get("keys", [])
    _emit(rows, as_json=as_json, table_fn=_print_keys)


def _print_keys(rows: list[dict]) -> None:
    t = Table(show_lines=False)
    t.add_column("alias", style="bold")
    t.add_column("key")
    t.add_column("monthly limit", justify="right")
    t.add_column("spend", justify="right")
    t.add_column("current?")
    for k in rows:
        lim = k.get("monthly_limit")
        t.add_row(
            k.get("alias", "—"),
            k.get("key_name") or "",
            f"${lim:.4f}/30d" if lim is not None else "—",
            f"${k.get('spend') or 0:.4f}",
            "✓" if k.get("is_current") else "",
        )
    _out.print(t)


@keys.command("create", help="Create a new API key.")
@click.argument("alias")
@click.option("--monthly-limit", type=float, default=None, help="Max USD this key can spend per 30 days.")
@click.option("--json", "as_json", is_flag=True)
def keys_create(alias: str, monthly_limit: float | None, as_json: bool) -> None:
    key = _require_key()
    body = {"alias": alias}
    if monthly_limit is not None:
        body["monthly_limit"] = monthly_limit
    with _auth_client(key) as c:
        r = c.post("/v1/keys", json=body)
    if r.status_code >= 400:
        _err.print(f"[red]{_extract_error(r)}[/red]")
        sys.exit(1)
    data = r.json()
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    _out.print(f"✓ Created key [bold]{data.get('alias')}[/bold]")
    _out.print(f"  {data.get('key')}")
    _out.print("[yellow]Copy it now — it won't be shown again.[/yellow]")


@keys.command("delete", help="Delete an API key by alias.")
@click.argument("alias")
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
def keys_delete(alias: str, yes: bool) -> None:
    key = _require_key()
    with _auth_client(key) as c:
        r = c.get("/v1/keys")
    if r.status_code >= 400:
        _err.print(f"[red]{_extract_error(r)}[/red]")
        sys.exit(1)
    matches = [k for k in r.json().get("keys", []) if k.get("alias") == alias]
    if not matches:
        _err.print(f"[red]No key with alias '{alias}'.[/red]")
        sys.exit(1)
    if len(matches) > 1:
        # Aliases aren't globally unique on the backend — picking matches[0] would
        # silently delete the wrong key. Ask the user to pick by key_name prefix.
        _err.print(f"[red]Multiple keys have alias '{alias}':[/red]")
        for m in matches:
            _err.print(f"  • {m.get('key_name')}  (spend ${m.get('spend') or 0:.4f})")
        _err.print("[yellow]Delete from the dashboard, or rename one first so aliases are unique.[/yellow]")
        sys.exit(1)
    target = matches[0]
    if not yes:
        click.confirm(f"Delete key '{alias}' ({target.get('key_name')})?", abort=True)
    with _auth_client(key) as c:
        r = c.post("/v1/keys/delete", json={"token": target["token"]})
    if r.status_code >= 400:
        _err.print(f"[red]{_extract_error(r)}[/red]")
        sys.exit(1)
    _out.print(f"✓ Deleted [bold]{alias}[/bold].")


# ────────────────────────── qsp usage ──────────────────────────

@main.command(help="Recent API calls with cost.")
@click.option("-n", "--limit", type=click.IntRange(min=0), default=10, show_default=True,
              help="Max rows in 'recent' table; 0 = hide recent, show totals only.")
@click.option("--json", "as_json", is_flag=True)
def usage(limit: int, as_json: bool) -> None:
    key = _require_key()
    with _auth_client(key) as c:
        r = c.get("/v1/usage")
    if r.status_code >= 400:
        _err.print(f"[red]{_extract_error(r)}[/red]")
        sys.exit(1)
    data = r.json()
    data["recent"] = (data.get("recent") or [])[:limit]
    _emit(data, as_json=as_json, table_fn=_print_usage)


def _print_usage(data: dict) -> None:
    totals = data.get("totals") or {}
    _out.print(
        f"Total: [bold]{totals.get('requests',0)}[/bold] requests · "
        f"{totals.get('tokens',0):,} tokens · "
        f"${totals.get('cost',0):.4f}"
    )
    by_model = data.get("by_model") or []
    if by_model:
        t = Table(title="By model")
        t.add_column("model", style="bold"); t.add_column("requests", justify="right")
        t.add_column("tokens", justify="right"); t.add_column("cost", justify="right")
        for m in by_model:
            t.add_row(m["model"], str(m["requests"]), f"{m['tokens']:,}", f"${m['cost']:.4f}")
        _out.print(t)
    recent = data.get("recent") or []
    if recent:
        t = Table(title="Recent requests")
        t.add_column("model", style="bold"); t.add_column("tokens", justify="right")
        t.add_column("latency", justify="right"); t.add_column("cost", justify="right"); t.add_column("at")
        for r in recent:
            t.add_row(r["model"], str(r.get("tokens", 0)),
                      f"{(r.get('duration_ms') or 0)/1000:.2f}s",
                      f"${r.get('cost',0):.6f}", r.get("at", "")[:19])
        _out.print(t)


# ────────────────────────── qsp status ──────────────────────────

@main.command(help="Check service status (routes + per-model latency).")
@click.option("--json", "as_json", is_flag=True)
def status(as_json: bool) -> None:
    with httpx.Client(base_url=DEFAULT_AUTH_URL, timeout=HTTP_TIMEOUT) as c:
        r = c.get("/v1/status")
    if r.status_code >= 400:
        _err.print(f"[red]{_extract_error(r)}[/red]")
        sys.exit(1)
    data = r.json()
    _emit(data, as_json=as_json, table_fn=_print_status)


def _print_status(data: dict) -> None:
    overall = (data.get("overall") or "unknown").upper()
    color = {"OPERATIONAL": "green", "DEGRADED": "yellow",
             "PARTIAL_OUTAGE": "yellow", "MAJOR_OUTAGE": "red"}.get(overall, "white")
    _out.print(f"Overall: [bold {color}]{overall}[/bold {color}]")
    t = Table(show_lines=False)
    t.add_column("model", style="bold"); t.add_column("status")
    t.add_column("latency (ms)", justify="right")
    for m in data.get("models") or []:
        t.add_row(m["model"], m["status"], str(m.get("latency_ms") or "—"))
    _out.print(t)


# ────────────────────────── qsp pay ──────────────────────────

@main.command(help="Open the Stripe checkout for a credit pack ($5 / $20 / $50).")
@click.argument("amount", type=click.Choice(["5", "20", "50"]))
def pay(amount: str) -> None:
    urls = {
        "5":  "https://buy.stripe.com/6oU9AT5eC2aGexN4t4gjC03",
        "20": "https://buy.stripe.com/6oU5kD0YmbLg9dt9NogjC04",
        "50": "https://buy.stripe.com/7sYbJ1ePc7v0ahxcZAgjC05",
    }
    url = urls[amount]
    _out.print(f"Opening checkout for [bold]${amount}[/bold]: {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass


# ────────────────────────── entrypoint ──────────────────────────

def run() -> None:
    """Entry point wrapper: invoke the click group with friendly handling for
    network / transport errors, so `qsp` never tracebacks on a DNS failure or
    firewall drop. Anything that's not a network error falls back to click's
    normal behaviour (usage errors, aborts, explicit SystemExit from commands)."""
    try:
        main(standalone_mode=False)
    except httpx.ConnectError:
        _err.print(
            f"[red]Can't reach QuickSilver Pro.[/red] "
            f"Check your internet / firewall, or see [bold]https://quicksilverpro.io/status[/bold]."
        )
        sys.exit(1)
    except httpx.ConnectTimeout:
        _err.print(
            f"[red]Connection timed out.[/red] "
            f"Network is slow or blocking — try again, or check [bold]https://quicksilverpro.io/status[/bold]."
        )
        sys.exit(1)
    except httpx.ReadTimeout:
        _err.print(
            f"[red]Request timed out waiting for a response.[/red] "
            f"The model may be cold — retry, or try a different model."
        )
        sys.exit(1)
    except httpx.HTTPError as e:
        # Catches WriteError, RemoteProtocolError, etc. — anything network-adjacent
        # that escaped the per-request handlers. Keep the message short; full repr
        # is often noisy and unhelpful to end users.
        _err.print(f"[red]Network error:[/red] {type(e).__name__}: {e}")
        sys.exit(1)
    except click.UsageError as e:
        e.show()
        sys.exit(e.exit_code)
    except click.ClickException as e:
        e.show()
        sys.exit(e.exit_code)
    except click.Abort:
        _err.print("[dim]Aborted.[/dim]")
        sys.exit(130)
    except KeyboardInterrupt:
        # Ctrl-C outside of a handled streaming loop. Exit 130 = canonical SIGINT.
        sys.exit(130)


if __name__ == "__main__":
    run()
