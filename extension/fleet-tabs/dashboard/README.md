# Server-driven result dashboard

Open the installed extension's dashboard/index.html in the local owner profile.
Enter the password configured locally with fleet.cmd owner-password and click
«Войти». The server verifies it with the existing rate-limited OwnerSessions
service and issues a random expiring dashboard-only bearer capability. The
capability is held in page memory, not localStorage; closing or reloading the
page requires logging in again. Public worker IDs are never credentials.
The view polls once every 8 seconds without refreshing the browser tab.
It renders only plain text nodes (`textContent`); never injects returned HTML.

## Server boundary and deployment

The extension already has local dev-mcp host permission and shares its existing
`fleet.js` MCP transport. TZ-24 registers one login and four session-guarded
MCP tools in the worktree `src/dev_mcp/server.py`; **the live server has NOT
been deployed** and the browser must refresh its tool list after the guarded
deployment. Each tool returns one JSON object as tool text:

- owner_dashboard_login(password): server-verified owner password -> short-lived
  opaque bearer token. The password is not stored in the page or returned.
- `owner_dashboard_overview({session_token})`: `owner.overview.snapshot(store)`.
  Read-only projection with no raw outcomes.spec/evidence_spec/authority, raw
  image paths, owner notes, worker logs, transcripts or credentials. A title is
  shown only when `spec.dashboard_visibility == "public"`.
- `owner_dashboard_gallery({outcome_id,session_token})`:
  `owner.taste_gallery.gallery_snapshot(store,outcome_id)`. Scope results to
  the authenticated owner's projects; never include `evidence.ref`.
- `owner_dashboard_choose({outcome_id,option_id,expected_revision,session_token})`:
  call `choose_taste(..., owner_authorized=True)` **only after the server
  has independently authenticated the actual owner and checked project access**.
  An argument provided by a client must never establish `owner_authorized`.
  The Python choice call is serialized in one SQLite transaction and writes
  to the existing shared DecisionBook tables with an evidence-version reference.
- `owner_dashboard_image({outcome_id,image_proof_id,session_token})`:
  authenticate the owner, verify that the image proof is allowed for this
  exact outcome and current subject version, and resolve the trusted artifact
  **on the server** (never an arbitrary client path). The evidence.ref must
  be an absolute allowlisted file in the owning project's root, with a
  supported extension and matching file signature. Return
  `{"mime":"image/png|image/jpeg|image/webp","base64":"..."}` with
  verified bytes <= 2 MiB. No raw private data or arbitrary files. Refuse
  missing/stale/failed proof; the UI disables a choice without preview.

All four data tools require server-minted dashboard session tokens. An existing
owner grant for a publicly visible W-ID cannot authenticate any dashboard call.
The random token is a bearer credential, so use a trusted local owner profile
and transport; do not share or log tokens. The tools cannot read
memory_service/Obsidian/owner logs. Prior to guarded deployment (or without
owner-grant), the UI renders offline/error state rather than falsely claiming
live data or enabling an unverifiable image choice.

## Data and semantics

`owner.overview.snapshot` is a read-only, consistent SQLite read transaction,
grouped by intent and outcome parents. It shows three **distinct** versioned
states: applied work/review, passed proof of current subject, and delivered
proof of current subject. A successful but stale proof is not current. Cyclic
or excessively deep parent links render as isolated entries with an error
instead of looping. No private data fields are returned.

The gallery is limited to open `owner_taste` obstacles and server-verified
`screen` evidence for the same outcome/version. It records one owner decision;
compare and choice controls do **not** approve result quality. Simultaneous
choices race via a database transaction, so exactly one can commit. Owner taste
choices are irreversible in this version; a replacement requires a new
versioned gallery decision, not a hidden overwrite.

`tests/owner/test_overview.py` and `tests/owner/test_taste_gallery.py` cover
empty/error states, privacy, proof-version staleness, loops, nonexistent images,
non-taste refusals, missing auth, CAS and simultaneous submissions.
