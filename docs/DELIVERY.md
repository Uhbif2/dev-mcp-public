# Fleet Platform — post-accept delivery (TZ-13)

Delivery is a separate state change **after** acceptance. A passing test or
review does not prove an artifact reached its target. Trusted server code calls
fleet.delivery.on_accept; this is NOT an agent-facing MCP tool. All effects
go through the separately authorized, budget-limited fleet_op Operator.
There are no shell commands, credentials or arbitrary script fields in policy.

## Project policy

Place deliver.toml at the project root. Without it delivery is disabled
(not_configured); an invalid policy fails closed (invalid_policy).
Example, replace workspace and hash values with real authorized revisions:

    [delivery]
    version = 1
    target = "staging"
    workspace = "C:/Projects/example"
    timeout_seconds = 60
    known_good_revision = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"

    [apply]
    operation = "cloud.deploy"
    [apply.params]
    workspace = "C:/Projects/example"
    target = "staging"
    artifact_sha256 = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    release_id = "release_1"

    [verify]
    operation = "ci.run"
    [verify.params]
    workspace = "C:/Projects/example"
    target = "staging"
    commit_sha = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    workflow = "postdeploy"

    [rollback]
    operation = "cloud.deploy"
    [rollback.params]
    workspace = "C:/Projects/example"
    target = "staging"
    artifact_sha256 = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    release_id = "known_good"

Allowed apply/rollback operations: cloud.deploy, android.build_install,
second_pc.run. Allowed verification operations: ci.run, eval.live,
device.screens, browser.task. Shared Operator-catalog validation rejects
unknown parameter names and unsafe types. All workspaces must be absolute,
consistent across steps, within server-provided authorized roots, with no
symlink path components. Rollback operation matches apply and targets exactly
the known-good revision. The accepted artifact hash must match apply input.

## Trusted server integration

Call on_accept with AcceptedEvent (event_id, project, subject_version,
env_version, evidence) and the server-owned arguments: project_root,
allowed_roots, state_db, fleet_op, probe_environment, create_defect.

The AcceptedEvent carries a passing server-issued review for that exact
subject AND environment, not an agent narrative. The fleet_op callback invokes
Operator.handle with registered operations and returns structured Operator
evidence: kind, ref, SHA-256 subject/environment versions, pass/fail,
producer=operator, operation and request_fingerprint. The environment probe
independently inspects the target and reports its current SHA-256 version.
Receipt validation requires the Operator's opaque opref:SHA256, nonnegative
observed_ts and safe numeric metrics; raw refs or adapter output must never be
included. For ci.run, verify.commit_sha must match the accepted subject digest
before any apply — checking a different commit is not delivery proof.
The create_defect callback must schedule repair work by a stable event key,
receiving original review proof and every verified delivery-stage proof.

**Integration status:** this package supplies the trusted entrypoint but
DOES NOT register the production acceptance hook; fleet/queue.py and the
controller routing are outside TZ-13 owns. A separate integration task must
call on_accept from the actual server acceptance transition and verify it
end-to-end (Fleet Platform/request:9a85c795 for styk:2). Until then automated
production delivery is NOT enabled.

## Delivery state and recovery

1. Before effects: validate policy and permitted paths, review evidence,
   artifact digest, target current environment version and timeout.
2. Reserve event_id in SQLite using BEGIN IMMEDIATE. Duplicate event returns
   prior receipt without repeat effects; different data under same event ID
   cannot create another deployment. A crashed in-progress receipt remains
   running and must be reconciled by a trusted controller, never blindly
   redeployed.
3. Apply using fleet_op, compare independently probed target version with the
   operator evidence; run explicit verification and compare versions again.
   Only passing apply + verify with stable version gives delivered status.
4. After any failed or uncertain apply attempt, invoke known-good rollback.
   A trusted defect callback receives all original evidence and the failure
   code, including when rollback also fails. Never report delivered after
   a failed verification. A queue failure records repair_pending.
5. The global timeout is checked before/after each operation; the Operator
   adapter MUST also implement its own timeout because an already-running
   external effect cannot be killed safely by this synchronous wrapper.

Receipt includes subject_version, expected_env_version, observed env_version
(only if verified), stage evidence, failure code and repair_work. Operator
proofs are filtered to safe structured fields. No secrets are read, logged,
or accepted in policy. This Python interface itself is not an OS capability
boundary and must never be exposed to the agent.

## Validation

Run: .venv/Scripts/python.exe -m pytest -q tests/fleet/test_delivery.py

Tests use synthetic operator/environment callbacks and cover missing or bad
TOML, symlinks, outside-roots workspace, unsupported operation, malformed
proof, duplicate accept, environment version drift, timeout, rollback failure,
repair queue failure and no false delivered state. A passing unit suite proves
the local contract, not an actual installation. Production integration needs
an independent accepted-event-to-Operator-to-live-probe execution trace.
