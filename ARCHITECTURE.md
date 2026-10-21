# Architecture

```
remote client (chat UI / any MCP client)
      │  MCP over HTTPS, token
gateway ──► mcp-proxy ──► dev-mcp (this repo, one Python process)
                            ├─ core: policy · files · git · exec · fetch · audit
                            └─ fleet: registry · queue · review · integrate · world
browser extension (extension/fleet-tabs) keeps the chat tabs of fleet workers alive
```

## Core (`src/dev_mcp/`)

- **`policy.py`** — the only door for a path or a command. `roots` (read), `write_roots`,
  `exec_roots`, `tree_roots`/`tree_deny`, `deny_patterns` matched per path segment. Every external
  path goes through `Policy.resolve()`; argv goes through `guard_exec_args()`. The file is
  re-read on change; a broken file is never applied.
- **`server.py`** — the MCP tools (FastMCP): read/write/patch with syntax check, read-only git,
  `run`/`run_task` (argv, no shell, scrubbed environment, background jobs), `fetch_url` with an
  SSRF guard. Responses are capped at 12 000 characters.
- **`audit.py`** — one JSON line per call (allow/deny, reason, duration), rotated daily.
- **`machine.py`** — every location outside the installation (workspaces, hub, live install,
  worktree base) in one place, overridable by `machine.toml`.

## Fleet (`src/dev_mcp/fleet/`)

A SQLite registry (`store.py`, WAL) of projects, tasks, workers, claims and an append-only
event log. Everything below reads and writes only through it.

| step | module | what happens |
|---|---|---|
| intake | `spec.py`, `queue_intake.py` | task specs (`ToDo/tz/TZ-*.md` with a YAML header: owns, needs, acceptance) become tasks |
| take | `queue.py`, `routing.py` | a worker gets the best runnable task and a claim on its `owns` paths |
| work | `claim_tree.py`, `checkpoint.py` | the claim works in its own git worktree; checkpoints make it resumable by another chat |
| submit | `queue.py` | only with green acceptance commands on the current files; the server seals the submitted bytes |
| review | `review.py` | another worker reviews blind against the seal; reject → `fix:` task, escalate → frozen with a reason |
| integrate | `integrate.py` | accepted bytes are committed after the project gate; drift, foreign writes or a red gate defer the work, never lose it |
| thaw | `reactivate_evidence.py`, `world.py` | a frozen task thaws only on trusted evidence: a deployment receipt, a resource sensor, an accepted dependency |
| oversight | `controller.py`, `watchdog.py` | stalled claims are requeued, chats are woken, loops are stopped and reported |

**Intents** (`src/dev_mcp/intents/`) turn an owner's free-text goal into a project with stages
and tasks. **Owner channel** (`src/dev_mcp/owner/`) mirrors the fleet onto a TickTick board:
one section per project, questions as checklists, a tick as a decision or an acceptance.

## Invariants worth knowing

- Nothing thaws on a timer or a free-text note — only on evidence the server itself verifies.
- Acceptance is re-run by the server; an agent's claim of "green" is never trusted.
- One writer per path: claims own paths, and the integrator commits only sealed bytes.
- Every refusal names its reason, so an agent can route around it instead of retrying blindly.

## Tests

`pytest tests -q` (~25 min, ~2 500 tests). Most of the core suite attacks the policy: `..`,
shared-prefix siblings, UNC, `\\?\`, ADS, junctions, 8.3 names, case.
