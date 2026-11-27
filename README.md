# dev-mcp

**Give this link to your coding agent (Claude Code or Codex):**

> `Set up dev-mcp from <this repository URL> following AGENT-SETUP.md`

The agent follows [AGENT-SETUP.md](AGENT-SETUP.md), performs the installation, and verifies a smoke task. You only need to:

1. Answer its startup questions and approve installation and any optional settings.
2. Sign in to your own ChatGPT account in the opened FleetChrome window.
3. Load the unpacked extension in Chrome and approve its permissions.
4. Complete the authorized tunnel and custom connector steps in the browser.
5. Resolve any remaining account/permission choice; enter secrets directly in the official UI or prescribed local credential file.

[User guide](GUIDE.md) · [Русский](README.ru.md) · [Manual installation](INSTALL.md)

A narrow MCP server that lets a remote agent (ChatGPT or any remote-MCP client) work on code on a
Windows machine — read, write, search, run tests — strictly inside folders an explicit policy file
allows. On top of the core sits **`fleet`**: a task registry, queue, blind review, acceptance and git
integration for a fleet of web-ChatGPT agents working in parallel.

> **Status: experimental, Windows-first.** Code comments and most of `docs/` are in Russian;
> the fleet assumes a remote gateway in front of the server. Issues are welcome.
> Russian README with more detail: [README.ru.md](README.ru.md).

## Core server

Everything the server may do is listed in `policy.toml` (start from `policy.example.toml`):

- `roots` — read · `write_roots` — write · `exec_roots` — run processes.
- `deny_patterns` are checked against **every path segment** (`.git` hides `.git/config`); denied
  files are not readable, listable or searchable.
- Path checks resist `..`, sibling folders with a shared prefix, UNC, `\\?\`, alternate data
  streams, junctions, 8.3 names and case tricks — that is what most of the test suite attacks.

| group | tools |
|---|---|
| read | `policy_info`, `list_dir`, `read_file`, `search_files` |
| write | `write_file`, `edit_file`, `apply_patch` (multi-file, all or nothing) |
| git (read-only) | `git_status`, `git_diff`, `git_log` |
| exec | `run`, `run_task`, `run_status`, `run_stop`, `run_list` |
| network | `fetch_url` (opt-in, SSRF guard on every redirect) |
| fleet | `fleet_*` — see `docs/FLEET-PROTOCOL.md` |

`run` takes an argv list, never a shell. `argv[0]` must be an allowed binary or a project-local
`.cmd/.bat`; `..` and absolute paths outside `roots` are refused in arguments; the environment is
scrubbed. Long runs move to the background and return a `job_id`.

**Honest boundary:** a folder that is both writable and executable means arbitrary code execution
for the client in that tree. The binary list prevents accidents and keeps the audit log readable;
the real perimeter is the gateway token plus the list of `exec_roots`. The spawned process is not
sandboxed (network or filesystem outside its cwd).

## Fleet (0.2.0)

- Scoped quick agents for small jobs; the queue remains the path for independent review and acceptance.
- Optional nightly code, product-gap and documentation review; results and proposed tasks remain under operator control.
- Optional conversation overseer with an authorized decision backend, disabled when none is configured: [overview](docs/OVERSEER.md).
- Improved task prerequisites, acceptance evidence, browser status and recovery, and queue/runtime efficiency.

Many ChatGPT conversations act as workers: they take tasks (`fleet_take`), work inside a claimed
zone of files, hand in with green acceptance commands, and are reviewed blindly by another worker.
Accepted candidates are validated before integration. Where enabled, they are committed and a full
gate checks an immutable checkout of the resulting Git revision. The owner can then retrieve an
immutable result bundle through the dashboard or `fleet.cmd result`; artifact availability,
deployment and owner approval are tracked separately. Frozen tasks thaw only on
trusted evidence (a deployment receipt, a world-resource sensor, an accepted dependency) — never
on a timer or a free-text note. Design and protocol live in `docs/`
(`FLEET-PROTOCOL.md`, `ROUTING.md`, `OUTCOMES.md`, `TICKTICK-BOARD.md`, …).

Architecture in one page: [ARCHITECTURE.md](ARCHITECTURE.md).

### Browser extension (`extension/fleet-tabs`)

An unpacked Chrome extension that keeps the chat tabs of fleet workers alive: it continues a tab
that holds a fleet task, recovers broken pages, wakes a silent conversation and stops everything
when the site shows an anti-abuse banner. It runs in **your own browser profile, against your
configured ChatGPT project**; it ships with an adapter for chatgpt.com. You are responsible for using it
within the terms of the site it runs on. Download the unpacked ZIP from
[Releases](../../releases), or use the source directory. Follow the
[extension installation guide](extension/fleet-tabs/README.md) to set your own
ChatGPT project and connector. Fresh installs start disabled.

## Running

For the guided Windows setup, local doctor, authenticated ChatGPT transport,
startup and removal, follow [INSTALL.md](INSTALL.md).

```powershell
uv sync --frozen --extra dev
copy policy.example.toml policy.toml
# Edit policy.toml to point at folders you own.
# Optional: copy machine.example.toml machine.toml and edit your locations.
.venv\Scripts\python.exe -m dev_mcp --check
.venv\Scripts\python.exe -m dev_mcp         # stdio MCP server
.venv\Scripts\python.exe -m pytest tests -q
```

Alternatively, create `.venv` with `py -3 -m venv .venv` and install with
`.venv\Scripts\python.exe -m pip install -e ".[dev]"`.
Python 3.11+ is required; the release is verified on Windows with Python 3.13.
The example grants writes and execution only inside `write_roots` and
`exec_roots`; `allow_write = false` keeps every other read root read-only.

For a fleet, copy `fleet-projects.example.toml` to `fleet-projects.toml` and
register your own projects using the [fleet protocol](docs/FLEET-PROTOCOL.md).
The public snapshot contains no existing projects, tasks, credentials, chat
history or production configuration. A small set of tests requiring the
maintainer's production configuration carries the `production` marker and is
explicitly skipped by the public `conftest.py`.

For remote access use the authenticated Secure MCP Tunnel in [INSTALL.md](INSTALL.md) or an explicitly configured authenticated gateway. Never expose the process-executing server without authorization.
For a local stdio client, adapt [mcp-client.example.json](mcp-client.example.json).
Every call is logged to `logs/audit.jsonl` (allow/deny, reason, duration; file contents are not
logged). The gateway, proxy launcher, model relays and personal integrations
are separate deployments; this repository does not install them. `restart.cmd`
is for operators who have configured their own proxy launcher via `machine.toml`;
for a standalone stdio server, restart it through its MCP client.

## License

MIT — see [LICENSE](LICENSE). Changes and release scope:
[CHANGELOG.md](CHANGELOG.md).
