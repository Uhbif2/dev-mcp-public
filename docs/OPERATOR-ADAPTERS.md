# TZ-29 — Operator-only named live adapters

## Trust boundary and deployment

These modules live exclusively in the restricted Operator OS process.
The fleet MCP server cannot instantiate remote drivers, read API credentials,
pick a host, configure a budget, install a browser profile or run a command.
The existing Operator validates operation catalog and approved owner budget
and writes its own ledger **before** invoking an adapter. The adapters add
atomic resource leases, checks for human activity and signed remote evidence.

Launcher entry point (paths provisioned with restrictive Operator-only ACLs):

    python -m dev_mcp.operator_cli --serve --budgets PRIVATE_BUDGET.toml --ledger PRIVATE_LEDGER.sqlite --adapters-config PRIVATE_ADAPTERS.toml

Without --adapters-config the Operator stays fail-closed. This is intentional.
Never point these paths at an agent-managed directory or a machine-owner Chrome
profile. No secret values or external hosts are received through fleet_op.

Example private configuration **without real credentials**:

    version = 1
    [remote]
    endpoint = "https://trusted-runner.example.invalid"
    token_file = "D:/operator-private/token"
    attestation_key_file = "D:/operator-private/attestation-key"
    issuer = "trusted-remote"
    lease_db = "D:/operator-private/leases.sqlite"
    profile_root = "D:/operator-private/browser-profiles"
    timeout_seconds = 30
    minimum_ram_mib = 512
    minimum_disk_mib = 512

    [browser_plans.oauth_setup]
    host = "accounts.example.invalid"
    mode = "configure"
    profile_id = "oauth-setup"
    instruction = "Configure the previously approved OAuth application."
    vault_slot = "private-oauth-setup"

    # Optional private screenshot bridge. Without [screens], device.screens
    # fails closed from the Operator CLI despite successful remote metadata.
    [screens]
    store_dir = "D:/operator-private/screens"
    store_key_file = "D:/operator-private/screen-store-hmac-key"
    capture_key_file = "D:/operator-private/capture-service-hmac-key"
    subject_version_file = "D:/operator-private/installed-build.sha256"
    env_version_file = "D:/operator-private/device-environment.sha256"

The trusted launcher must provision directories before starting the process,
protect the config/token/signature key with filesystem ACLs, and run from an
OS identity distinct from the user's everyday browser. The protected budget
must separately allow the target, workspace root and browser host.

## Remote service contract

The gateway calls only an allowlisted path with one of seven exact operation
names (android.build_install, device.screens, cloud.deploy, eval.live,
ci.run, second_pc.run, browser.task):

- GET /v1/probe/{operation} → ResourceSnapshot(resource_id, online,
  human_using, free_ram_mib, free_disk_mib). For Android this MUST use an
  already running adb server/daemon sensor, **not start an adb client**.
  For second PC it MUST check the person's active session before an effect.
- POST /v1/run/{operation} → BackendReceipt with operation, request_sha,
  resource_id, subject_version, env_version, opaque ref, outcome,
  issuer, HMAC-SHA256 signature and operation-specific proof fields.
  Request body includes params and, only for browser.task, a private
  isolated_browser_profile, trusted_goal and vault_slot from the trusted
  plan registry. The server must implement each action using a fixed driver,
  not arbitrary shell, Python code, browser instruction or user-provided
  executable.
- POST /v1/readback/cloud.deploy → exact deployed artifact_sha256;
  POST /v1/rollback/cloud.deploy → restoration status. A readback mismatch
  invokes rollback and records failure even if rollback itself fails.

The receipt HMAC is computed over the canonical JSON SHA-256 of all
BackendReceipt fields **except signature**; hexadecimal lowercase SHA-256
over the digest's ASCII bytes with the pinned, Operator-private symmetric key.
Issuer is pinned. The signer and operator have access to the protected key;
fleet agents do not. A model's verdict or a raw HTTP 200 cannot substitute
for a correctly signed receipt. The Operator returns a filtered, hashed opref,
never the raw result or credential. The HTTP bridge disables Windows/system
proxies, rejects redirects, enforces response and timeout limits, and requires
TLS except for loopback.

Operation-specific outcome rules:

- Android install must attest the requested artifact SHA; device screens
  must attest the number of screenshots. Both depend on an idle, online
  resource; no device access on failed probe.
- Cloud deploy must return the requested artifact SHA, then pass readback.
  Any signed remote `fail`/`needs_human` outcome also attempts rollback, because
  the remote service may already have changed state. A broken deployment is a
  failed verdict with rollback attempted; repeated
  fingerprint is handled by the durable ledger, not blindly redeployed.
- eval.live executes a pinned corpus by hash and sample count, with only
  aggregate metrics returned; no prompt, document or secret leaves through
  public evidence. The trusted service selects the allowed model provider,
  never Claude or Codex.
- ci.run pins commit SHA and reports source/env digests, fresh workspace
  and user-visible E2E. second_pc.run pins artifact SHA with fresh and
  user-visible markers and requires human_using=false. A timeout is failure.
  For stronger end-to-end proof, TZ-19 separately verifies independent
  attested clone/install/run stages.
- browser.task uses a **trusted pre-registered plan**. It must be implemented
  by the separately provisioned Jev Browser library at
  C:/hub/jev-browser, running under the Operator identity with a separate
  profile. Follow its docs/LOCAL_AGENT.md contract: browser_run result
  with UI readback, browser_handoff when login requires the owner, and
  browser_secret_capture + protected vault writing without surfacing
  secret values. API credentials stay only in Operator's vault; a login
  requiring a person returns outcome=needs_human, not pass.

## Trusted screen pixels and visual lenses

The Operator launcher installs the private screen bridge before creating
named adapters, and only in its dedicated OS process. An absent [screens]
section denies device.screens, rather than accepting metadata as pixels.
Both HMAC keys are different, private binary values of at least 32 bytes.
The store root must already exist and be private to the Operator/verifier
identity. The launcher must provision and audit restrictive Windows DACLs
that deny ordinary users, browser and fleet identities; the module does
not create or verify Windows DACLs itself. POSIX roots additionally reject
group/other permission bits. No secrets are sourced from environment or
fleet_op.

The reserved, independently authenticated remote capture driver must
return a signed backend receipt with fresh=true, current subject and
environment digests, matching request and screenshot count. Then the
protected gateway uses POST /v1/capture/device.screens with exactly
receipt_ref, receipt_request_sha, execution_id (never a caller-selected
path or URL). The response has two keys: payload and hmac. Payload has
receipt_ref, receipt_request_sha, subject_version, env_version and frames
(1-10 base64 PNG/JPEG byte strings). The hmac is lowercase hexadecimal
HMAC-SHA256 of canonical JSON payload, using the independent capture key
rather than backend receipt or private store key. The private gateway
caps this specific response at 60 MiB. The remote capture service must
use a real reserved device, not synthetic images or metadata alone.

The version files are maintained independently by trusted deployment and
device sensors. They are read on every capture and private retrieval;
changing them invalidates stale proofs. Frames are persisted as immutable
signed private bundles. Public Operator responses contain only opaque
opref and digests, never pixels.

Local privileged visual verification uses --visual-lens with one of
visual_motion, error_empty, first_run; also provide --visual-intent-file,
--visual-workspace, --visual-target and the normal private budget, ledger,
adapter config. It constructs LensRunner with the actual Operator.handle
and the private loader inside the OS-restricted process. Only a bounded
status, clean streak and obstacles are returned; no pixels. Without
capture driver, rubric, Gemini, fresh proof or budget, it blocks.
This private loader is not a fleet-facing MCP API.

## Reservations and failure handling

AdapterRuntime uses SQLite BEGIN IMMEDIATE to insert a per-resource exclusive
lease BEFORE the first remote effect. A second job targeting the same phone,
CI runner or PC is denied; completed, version-bound results are cached by
request fingerprint and SHA. A crash leaves the reservation charged rather
than risking duplicate deploy/install; trusted reconciliation is required.
Unavailability, capacity failure, missing signature, bad hash, API exceptions,
secret-bearing error bodies and CI timeouts produce a structured fail result,
not a claimed success. Failures never echo raw driver strings. The Operator
then enforces budget/time/call caps independently.

## Verification status

The runnable hermetic acceptance uses fake probes, signed fixture receipts,
fake Jev bridge, simulated API failures, parallel leases, and real
Operator.handle budget enforcement. It also tests the integrated
Operator.handle -> signed synthetic capture -> private store -> LensRunner
path and rejects tampered or stale proofs:

    .venv/Scripts/python.exe -m pytest -q tests/operators/adapters

**No actual Android device, capture endpoint, Windows ACL, live Gemini,
cloud account, CI runner, second PC or login is
contacted during this acceptance.** The live transport and Operator CLI
wiring are implemented but require separately provisioned remote services,
an Operator OS identity, approved budgets, private keys and actual Jev-backed
driver integration before claiming real-world success. Absence of those
resources remains fail-closed.
