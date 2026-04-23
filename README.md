# QuickSilver Pro CLI

`qsp` — a command-line client for [QuickSilver Pro](https://quicksilverpro.io), an OpenAI-compatible inference API for top open-source LLMs (DeepSeek V3, DeepSeek R1, Qwen 3.5) priced 20% below OpenRouter / Together / Fireworks.

Designed to be **AI-agent friendly**: every command accepts `--json` for structured output, exit codes are reliable, and the API surface is intentionally small.

---

## Related repos

QuickSilver Pro is developed in three repositories under [`machinefi`](https://github.com/machinefi):

| Component | Repo | Visibility |
|-----------|------|------------|
| **CLI** — `qsp` command-line client (this repo) | [`qspro-cli`](https://github.com/machinefi/qspro-cli) | Public |
| Backend — API gateway + billing | [`qspro-backend`](https://github.com/machinefi/qspro-backend) | Private |
| Frontend — landing + dashboard | [`qspro-frontend`](https://github.com/machinefi/qspro-frontend) | Private |

End-user site: <https://quicksilverpro.io>.

---

## Install

```bash
pip install quicksilverpro
```

Python 3.9+. Also exports itself as `quicksilverpro` if you prefer the long name.

---

## Quick start

```bash
qsp init                     # opens dashboard to get a key, stores it locally
qsp chat "Write me a haiku"  # one-shot streaming chat (deepseek-v3 by default)
qsp balance                  # current credits
qsp models                   # supported models with prices & context length
qsp status                   # live per-model latency
```

---

## Commands

| Command | Purpose |
|---|---|
| `qsp init [--email X] [--key sk-...]` | Sign in (browser walkthrough) or paste an existing key |
| `qsp logout` | Forget locally-stored key |
| `qsp whoami [--json]` | Show signed-in email + balance |
| `qsp balance [--json]` | Credit balance + lifetime spend |
| `qsp models [--json]` | Available models + pricing + context length |
| `qsp chat "PROMPT" [-m MODEL] [-s SYS] [--max-tokens N] [--temperature F] [--no-stream] [--json]` | One-shot completion, streams to stdout by default |
| `qsp usage [-n 10] [--json]` | Recent calls + aggregate per-model |
| `qsp status [--json]` | Live health of API + per-model probes |
| `qsp keys list [--json]` | Your API keys |
| `qsp keys create ALIAS [--monthly-limit USD] [--json]` | Create a new key with optional spend cap |
| `qsp keys delete ALIAS [-y]` | Delete a key (confirmation prompt unless `-y`) |
| `qsp pay {5,20,50}` | Opens Stripe checkout for a credit top-up |

---

## AI-agent usage

Every command supports `--json` and prints OpenAI-shaped JSON to stdout with errors on stderr.

```bash
qsp models --json | jq '.[].id'
qsp usage --json  | jq '.totals.cost'
qsp chat "Summarize: $DOCUMENT" --json --no-stream | jq -r '.choices[0].message.content'
```

Exit codes: `0` success · `1` remote/operational error · `2` usage / auth error.

---

## Config

Key stored at `~/.config/quicksilverpro/config.json` (chmod 600). Override with:

- `QSP_API_KEY` — use this key directly, ignore stored config
- `QSP_API_URL` — default `https://api.quicksilverpro.io/v1`
- `QSP_AUTH_URL` — default `https://pay.quicksilverpro.io`
- `QSP_MODEL` — default model for `qsp chat`
- `QSP_CONFIG_DIR` — where to store config
- `QSP_HTTP_TIMEOUT` — seconds, default 60

---

## Use the `openai` SDK directly

You don't need this CLI to use QuickSilver Pro. The OpenAI Python / Node / Swift SDKs work with only a `base_url` change:

```python
from openai import OpenAI
client = OpenAI(
    base_url="https://api.quicksilverpro.io/v1",
    api_key="sk-...",   # your QuickSilver Pro key
)
r = client.chat.completions.create(
    model="deepseek-v3",
    messages=[{"role": "user", "content": "Hello"}],
)
```

See [quicksilverpro.io/dashboard#quickstart](https://quicksilverpro.io/dashboard#quickstart) for JS / Swift / curl.

---

## License

MIT.

QuickSilver Pro is a product of MachineFi Inc. (68 Willow Rd, Menlo Park, CA).

Links: [home](https://quicksilverpro.io) · [status](https://quicksilverpro.io/status) · [terms](https://quicksilverpro.io/terms) · [privacy](https://quicksilverpro.io/privacy)
