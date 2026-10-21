# Intent sources: TZ-15 contract

The input boundary is `dev_mcp.intents.sources`. It is a library, **not** an enabled
background scanner, a controller integration, or an authorization broker. The
controller (separate work) must instantiate the adapters, configure authority,
supply its own SQLite connection, and consume only the emitted `IntentChange`
events. Nothing here creates project trees or reads live private accounts.

## API and durable identity

`SourceAdapter.poll() -> PollResult(source_id, envelopes, complete, errors)`.
Every `IntentEnvelope` has `source_id`, stable `external_id`, `revision`,
`raw_intent`, `authority` and source `timestamp` (ISO-8601). The stable identity
is **(source_id, external_id)**, not `external_id` alone. `revision` includes
exact UTF-8 content, trusted authority and optional provider revision.
Timestamp alone does not create a new revision. The source text is retained
verbatim (including original line endings); it is never normalized, trimmed,
or used to confer authority. Empty/invalid/oversized inputs are rejected.

`IntentRegistry(sqlite3_connection)` installs its additive table
`intent_source_revisions` with primary key `(source_id, external_id)`.
`registry.poll(adapter)` or `registry.reconcile(snapshot)` returns
`ReconcileResult(applied, changes, errors)`. Only a **complete, clean** source
snapshot is committed. Changes have typed `kind`:

- `created`: unseen key, or a previously cancelled key reappears;
- `updated`: active key with a changed revision or trusted authority;
- `cancelled`: active key disappears from a successfully polled source;
  the last raw text and revision remain in the event and database.

The same active revision emits **zero** changes, even after reopening the
SQLite database. A partial scan, missing source directory, network outage,
disabled source, or any invalid file does not alter the database and cannot
cancel previously seen work. The consumer must use signals to reconcile the
controller's desired state. It must **not spawn a new outcome tree** for an
identical revision or treat a failed poll as deletion. Changes are committed
transactionally in SQLite with a durable outbox. `IntentChange.event_id` is a
stable monotonic delivery ID. The controller must call
`registry.pending_changes()` on startup and every reconciliation tick, process
each event idempotently by `event_id` and `(source_id, external_id, revision)`,
then call `registry.ack_change(event_id)` **after** downstream persistence.
The ACK uses the caller's SQLite transaction and must be committed by the
caller (for example, by surrounding outcome write + ACK with `with db:`).
Unacknowledged events survive restarts and remain replayable without spawning
another tree on each poll. For an exactly-once local write, share SQLite and
couple the consumer's outcome write and ACK in the same transaction.

## Adapter boundaries

* `FileIntentAdapter` defaults to **C:/hub/_todo/intents/**, and examines
  only its direct children with `.md`, `.txt`, `.intent` or no suffix.
  Hidden/temp names are ignored; symlinks are rejected. Files are read as
  strict UTF-8 and capped at 1 MiB by default (configurable). The root must
  exist as an actual directory; an unavailable root is not an empty scan.
  The source ID for another configured root must be supplied explicitly:
  otherwise construction fails with `ValueError` (no scan occurs). Different
  custom roots must use distinct, persistent IDs, including across restarts;
  `file:C:/hub/_todo/intents` is reserved for the default root and cannot
  be reused by a custom root. The default root retains this ID automatically.
* `ObsidianIntentAdapter(vault_id, list_marked=..., read_marked=...)` requires
  a provider that searches **only structurally tagged metadata** (default
  exact tag `owner-intent`, optional `#` prefix), not a raw full-vault dump.
  It reads note bodies **only after validating exact metadata tags**. A tag
  embedded in text, a substring, or an unmarked note grants no access.
  The injected callbacks must represent the authorized scoped search API.
* `TickTickIntentAdapter(account_id, ...)` uses the same metadata-first
  boundary but is **disabled by default**. To enable it, the operator must
  explicitly establish owner-granted access, provide scoped callbacks, and
  construct the adapter with both `enabled=True` and `owner_granted=True`.
  These booleans are assertions by the trusted operator, **not proof of OAuth**.
  Never request access for tests; inject fake providers instead. An offline
  TickTick endpoint fails closed, preserving prior registry state.

`authority` is injected by the trusted operator/configuration, not extracted
from task titles, note bodies, frontmatter, inline labels, or API response text.
Do not insert account secrets, OAuth tokens, or personal credentials into
`authority` or the SQLite journal. Scanning/polling is read-only; it must not
write to the live source folder, Obsidian, or TickTick.

## Minimal consumer usage

```python
import sqlite3
from dev_mcp.intents.sources import FileIntentAdapter, IntentRegistry

db = sqlite3.connect('operator-owned-controller.sqlite')
registry = IntentRegistry(db)
changes = registry.poll(FileIntentAdapter(authority={'allowed_roots': []}))
if changes.applied:
    for change in changes.changes:
        # Dispatch by change.kind and change.key to the outcome controller.
        # Persist downstream acknowledgement before marking delivery complete.
        pass
```

Integration warning: the controller must consume/ACK the outbox and reconcile
persisted desired state; this library alone is not a running service. Green unit
tests demonstrate the adapter contract only, not deployment to the live system.

Acceptance: `.venv/Scripts/python.exe -m pytest -q tests/intents/sources`
uses temporary files, fake marked providers and temporary SQLite only.
