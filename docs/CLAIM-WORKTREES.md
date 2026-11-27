# Fleet claim worktrees — core contract (TZ-34)

**Исторический контракт TZ-34; действующий канон — `FLEET-PROTOCOL.md`, `CLAUDE-CODE-FLEET.md` и модули `src/dev_mcp/fleet/claim_*.py`.** Маршрут `ClaimWorktrees` подключён: `claim_tree.py` реализует worktree-механизм; `claim_take_routing.py`, `queue.py` и `tools.py` обслуживают выдачу/маршрутизацию claim и операций; `claim_review_bite.py` — ревью и bite; `claim_accept_flow.py` и `integrate.py` — принятие/интеграцию; `claim_maintenance.py` — обслуживание; `controller.py` учитывает флаги маршрута. Он включается **только одновременно при `commit = true` и `claim_trees = true`**. На 17.11.2026 ни у одного проекта в `fleet-projects.toml` нет `claim_trees = true`, поэтому изолированные claim-worktree сейчас **выключены во всех проектах**, используется общий маршрут.

**Decision:** trunk contains accepted results only; deployment reads trunk.
The queue, review/bite, accepted-only integration and maintenance routes were wired
**after TZ-34**. The lifecycle below records the original design-stage contract;
current execution depends on the project feature flags above.
Do not point an agent at the shared trunk and call it an isolated claim.

## Storage and identity

For a project with both `commit = true` and `claim_trees = true`,
`ClaimWorktrees(project_root).open(id, base)`
creates a Git worktree with branch `claim/<safe-id>` at
`<project_root>/../.fleet-trees/<project-directory>/<safe-id>`.
Top-level `open(project_root, claim_id, base="HEAD") -> Path` is equivalent.
`base` must resolve to the **current trunk HEAD**; a stale base fails closed.
Retries for an already-associated path are idempotent. Simple safe claim IDs
are preserved; unsafe IDs (including colons, slashes and Windows-special names)
get a stable collision-resistant suffix. Paths with Cyrillic and spaces work.
Never copy dirty trunk files into the claim tree.

The project virtual environment remains in `<project_root>/.venv`; **do not
duplicate it** for each task. Run its Python executable with the claim tree as
cwd and `PYTHONPATH=<claim_tree>/src` (see `python_environment`). Other
language runtimes should use the same source-tree isolation principle.

## Lifecycle

1. Trusted queue owner verifies `commit = true` **and** `claim_trees = true`,
   captures trunk commit SHA and uses `open(root, id, sha)`; binds claim id →
   worktree path/branch in durable queue state. If either flag is disabled,
   continue the existing shared-tree behavior (this module is not invoked).
2. All file tools, command cwd, review and bite tests for this claim must
   operate in **its** worktree. Paths, file events and acceptance freshness
   checks must be translated into that tree. The trusted post-accept consumer
   stages **only accepted owned paths**, commits them **in the claim branch**,
   and verifies the accepted event. Do not commit all staged/untracked files.
3. Only after verified acceptance, call
   `integrate(Claim(root, id, accepted=True), gate=gate_on_trunk)`.
   The flag is a trusted assertion by the queue/integrator after verified
   acceptance, not an authorization handed to a web agent. No acceptance → no integration.
   Dirty claim or dirty trunk → refusal. If the trunk advanced, the claim
   branch is rebased onto its current HEAD, then Git fast-forwards trunk.
   Rebase conflicts → `IntegrationResult(status="needs_result",
   conflict_paths=(...))`, with rebase aborted and trunk untouched.
   Caller persists a `needs_result` dependency naming these paths and
   opens a repair task; it must **not** auto-resolve conflicts or force merge.
   After repair, re-review and re-run acceptance on the resulting claim tree.
4. The project gate runs **on trunk after the fast-forward**, never on other
   unfinished worktrees. A failing gate yields `gate_failed` with `gate_rc`;
   the merged commit remains for the separate styk/recovery process (no silent
   unreviewed rollback). If no gate callback was supplied, caller must run
   and persist the usual project gate before deployment.
5. `close(Claim(root,id))` removes a clean merged worktree and its branch.
   Unmerged/dirty work is protected; only an explicitly authorized
   `close(..., abandon=True)` discards it. Maintenance can invoke
   `cleanup([(claim_id, terminal_state, finished_at)], older_than=cutoff)`:
   terminal `accepted` or `abandoned` **and** age below cutoff are both
   required. Never prune a `taken`, under-review, repair or unknown claim.

Open, integrate and close use an exclusive per-project lock under
`.fleet-trees`. An interrupted process may leave the lock: diagnose the
owner before clearing it; do not steal it on a timer. Git refs and tree files
are not rewritten on merge conflict. No live worktree is created by unit tests.

## Follow-on implementation tickets (historical TZ-34 plan; wiring subsequently implemented)

1. **TZ-35 — Queue worktree routing:** issue/open worktree per claim in
   `fleet_take` for `commit=true`, persist root/base/path, route
   `read_file`/`write_file`/`edit_file`/`apply_patch`/`run` and
   normalized `owns` paths to that worktree; preserve `commit=false`.
2. **TZ-36 — Review and bite isolation:** bind independent reviewer/test cwd
   and old/new bite snapshots to the exact claim tree and accepted revision;
   invalidate prior acceptance on any later change.
3. **TZ-37 — Accepted-only integrator:** verify durable accept, commit ONLY
   accepted owned paths in the claim branch, call `integrate`, persist
   merge and `needs_result` with paths, create repair task and run
   the project gate on the merged trunk before release.
4. **TZ-38 — Tree maintenance and recovery:** terminal state + age cleanup,
   recovery after restart, stale integration-lock diagnosis, branch/worktree
   audit, and canary deploying *only* the accepted trunk.

These numbers were **proposed tickets**, not evidence of their bootstrap under these IDs.
Use the current modules and project flags above to determine the actual execution route.
