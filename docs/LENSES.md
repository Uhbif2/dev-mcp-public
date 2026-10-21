# Quality lenses: versioned, independently evidenced (TZ-18)

Seven stable checks against the **original intent**, not merely a green build:

| Lens | Required scenarios | Trusted evidence |
| --- | --- | --- |
| `visual_motion` | home, transition (3 distinct frames) | Operator `device.screens` + actual pixels + `gemini.look` |
| `error_empty` | error, empty | Operator capture + Gemini |
| `first_run` | onboarding (2 frames) | Operator capture + Gemini |
| `performance` | startup_perf | Server profiler `measure` proof |
| `security` | denied_access | Server negative-access `run` proof |
| `documentation` | getting_started | Independent server `review` proof |
| `recovery` | restore | Server failure/recovery `run` proof |

The public Operator `device.screens` response contains an *opaque evidence
reference* (opref), subject/environment hashes, request fingerprint and a
timestamp, **not image bytes**. A separately deployed trusted receipt-indexed
screen store must supply `Frame` objects. The loader runs inside the trusted
server, reads actual image bytes (not a user- or agent-supplied caption), binds
them to the exact opref, subject version, environment version and named
scenario, then provides the bytes to Gemini. A missing loader or missing bytes
is an obstacle. No assumption that every capture automatically comes with a
downloadable public screenshot.

## Contract

`LensRunner(intent, subject_version, workspace, target, operator_call,
frame_loader, check_provider, rubric_paths, look)`. `subject_version` is the
64-digit lowercase SHA-256 of the actually tested build. The intent is hashed
and its content supplied as comparison criteria (untrusted quoted data).
`operator_call(operation, params)` must be the trusted fleet Operator path,
not an agent-written fake response. `frame_loader(operator_proof, scenario)`
must be a privileged resolver of the same authenticated screenshot receipt.
Do not expose the resolver or its raw paths through a public MCP method.
`check_provider(case, version)` must be a **server verifier** or actual
Operator-backed test, not model prose or a CI success string. `CheckProof`
includes kind, scenario, producer, subject_version, env_version, reference,
test verdict and detail. A server provider MUST actually apply the criterion
(such as a measured latency limit, an unauthorized request or recovery drill)
before returning `pass`. The runner validates provenance/versions and
consecutive passes, but cannot reproduce the provider's benchmark or policy.
Nonvisual proof references must be unique for each verification: replaying an
earlier `CheckProof.ref` blocks the lens rather than counting as a second pass.

`run_once(lens)` captures **each** required scenario anew with a fresh
`execution_id` (same request id would be cached by Operator). It checks the
public Operator schema, actual request fingerprint, opref replay, version,
scenario, MIME/hash and PNG pixel data. Animation/first-run sequence requires
**every** requested frame to have distinct image content (three of three for
`transition`, not merely two unique frames). JPEG support checks segment structure before Gemini decodes;
the local preflight is not a substitute for image decoding or Gemini. The
rubric is read from canonical `C:/Users/dev/memory_service/skills/art-director/SKILL.md`
or project `.claude/skills/art-director/SKILL.md`. Missing, inaccessible or
empty rubric => `art_director_unavailable` obstacle; **no synthetic fallback**.
For every visual case, `gemini.look(images, rubric + intent + scenario)` must
return a consistent `ok` and list of defects with P0..P3, location and detail.
A relay error, bad image, absent frames, tampered label/version or conflicting
Gemini fields => blocked, never done.

`report(lens)` records immutable observations with Operator opref, SHA256 for
each actual frame, frame dimensions, request fingerprint, subject/environment
version, rubric and original-intent hashes, model verdict and defects. To close,
**all scenarios must produce no defects in two consecutive, fresh runs**.
Two contradictory runs are `unstable`; an earlier clean round alone never
closes a lens. Any P0/P1 visual or nonvisual defect is still a defect even when
unrelated unit tests/CI passed. `create_defect_outcomes(store, report, ...)`
persists P0/P1 as deterministic, idempotent **OPEN** outcome records with
versioned specs and observable defect obstacles. They are not automatically
closed: correction must be followed by independent clean evidence on the
appropriate version. P2/P3 do not create corrective outcomes automatically
but still prevent lens closure.

The module **does not claim production wiring**: the present Operator public
contract does not return pixels, and this task does not own Operator adapters,
the controller, intent registry or owner dashboard. Production use requires
an authorized screen-receipt store and the controller to invoke
`LensRunner` / outcome materialization, including trusted nonvisual verifiers.
Until those are wired, the state is `blocked` / not observed, not verified.
Canonical art-director missing or access-denied in this execution environment
must likewise stay an explicit obstacle.

Unit acceptance:
`.venv/Scripts/python.exe -m pytest -q tests/fleet/lenses`.
The test harness uses a **real Operator budget, ledger and screen proof** and
actual decompressed PNG pixels, but an injected Gemini evaluator instead of
calling the live relay; therefore green tests establish the contract only,
not that production capture, the relay or the whole application is healthy.
