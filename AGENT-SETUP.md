# Set up dev-mcp for your human (Windows)

**You, the coding agent, do the work.** Ask the human only for the HUMAN steps below. Ask all startup questions together before installing anything. Do not request tokens, passwords or recovery codes in chat: the human enters them directly into the official UI or the local credential file prescribed by that tool. Never print or copy credentials into logs, task specs or Git.

Work on a machine and in projects the human owns or is explicitly authorized to administer. A directory that permits both writes and execution grants code execution as the server's OS user, not a sandbox. Keep the installation, personal files and credentials outside the worker workspace. Read [SECURITY.md](SECURITY.md) and [INSTALL.md](INSTALL.md).

## HUMAN (the only steps requiring the person)

1. HUMAN — Answer in one message: installation folder (default %USERPROFILE%\dev-mcp), separate fleet project workspace (default %USERPROFILE%\fleet-work), and ChatGPT plan/workspace (does it permit custom MCP connectors/Developer mode?); approve the prerequisites, and say whether to allow optional global coding-agent instructions and logon startup.
2. HUMAN — Sign in to your own ChatGPT account in the opened FleetChrome window; complete any account or human-verification prompts yourself.
3. HUMAN — In that Chrome profile, open chrome://extensions, enable Developer mode and choose Load unpacked; paste the prepared folder path. Review the requested extension permissions.
4. HUMAN — Complete Secure MCP Tunnel and ChatGPT connector authorization in the official browser UI, paste any credentials directly into the prescribed local UI/file, and supply the non-secret project URL and connector display name; paste the prepared project instructions into the ChatGPT project.
5. HUMAN — Decide only on any unresolved permission, account or optional integration choice (including TickTick OAuth); never grant a permission merely to make a failing check pass.

## 1. Prerequisites and checkout

- **Do:** Check git --version, py -3 --version (Python 3.11+ with launcher), optional uv --version, and whether Google Chrome is installed. With HUMAN approval, install only missing prerequisites using winget install --id Git.Git -e, winget install --id Python.Python.3.13 -e, winget install --id Google.Chrome -e; optional winget install --id astral-sh.uv -e. Check package IDs with winget search first if the catalog differs. Do not elevate privileges without explicit approval.
- **Verify:** Open a fresh shell and repeat the version checks. If installation fails, report the exact package/exit code and do not proceed with missing prerequisites.
- **Do:** Clone the supplied repository URL into the agreed installation directory. For an existing checkout, verify its origin and current branch, preserve local edits and configurations, and do not reset or reclone over it. Use a separate, initially empty workspace for the first project.
- **Verify:** The checkout has tools/setup.ps1, tools/standalone.py, policy.example.toml and extension/fleet-tabs/manifest.json. If any are absent, stop and check the supplied release/ref.

## 2. Initialize and verify locally

Run from the installation directory in PowerShell; assign $Workspace to the agreed absolute workspace path. Use the actual commands from [INSTALL.md](INSTALL.md):

```powershell
powershell -NoProfile -File tools/setup.ps1 -Workspace "$Workspace" -Project Demo
.venv\Scripts\python.exe tools/standalone.py start --core-only
.venv\Scripts\python.exe tools/standalone.py doctor
```

- **Verify:** setup creates the venv, a narrow policy, machine configuration and one registered Demo project. Doctor must exit zero with ready, initialize and policy_info true. Inspect allowed roots, not secrets.
- **Repeat safely:** If the three local configuration files already exist, do not rerun init or overwrite them; use the existing configuration-only doctor and check that it is the intended installation. If configuration is partial, stop and reconcile manually with the human; never delete existing data. If a port is occupied by a different service, do not kill that service.
- **Do:** Only after the core check, switch this installation to full mode:

```powershell
.venv\Scripts\python.exe tools/standalone.py stop
.venv\Scripts\python.exe tools/standalone.py start
.venv\Scripts\python.exe tools/standalone.py status
.venv\Scripts\python.exe tools/standalone.py doctor
```

- **Verify:** ready is true and the instance is running. An existing full-mode instance need not be restarted. If either probe fails, inspect logs/standalone.log and correct the specific issue; do not broaden policy or bind the listener to a public interface.

## 3. Authenticated ChatGPT connection

- **Do:** Follow the current [Secure MCP Tunnel quickstart](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels), install its official client as instructed, then run tunnel-client help quickstart. Use the HTTP profile with --mcp-server-url http://127.0.0.1:8766/servers/dev/mcp. Keep the authorized tunnel client running. Direct the human to the official account/workspace authorization and connector steps (HUMAN 4). Do not use another operator's account or credentials.
- **Verify:** Run the tunnel's own doctor, connect the custom MCP connector to the intended ChatGPT workspace, and ask ChatGPT for policy_info from a test conversation. The result must refer to the intended machine and narrow workspace.
- **If blocked:** A plan without the required custom connector/tunnel support cannot run the remote fleet; keep the local setup working and report the missing entitlement. Never expose this process-executing server through an unauthenticated public tunnel or public port. Browser limits and human-verification prompts require human action, not automation.

## 4. FleetChrome and extension

- **Do:** Start the dedicated browser from the install directory: .venv\Scripts\python.exe tools/fleet_chrome.py start. It uses %LOCALAPPDATA%\FleetChrome, not the person's usual Chrome profile; local DevTools listens on port 9223. Do not expose that port. Bring the FleetChrome window forward for HUMAN 2.
- **Verify:** .venv\Scripts\python.exe tools/fleet_chrome.py status reports ready; if not, check Chrome installation and whether the port is occupied by an unrelated process. Do not terminate an unrelated browser.
- **Do:** Open the extension folder in Explorer and copy its absolute path to the clipboard (PowerShell: Resolve-Path extension/fleet-tabs | Select-Object -ExpandProperty Path | Set-Clipboard). Ask the human to perform HUMAN 3. Follow [Fleet Tabs instructions](extension/fleet-tabs/README.md).
- **Do:** With the non-secret values from HUMAN 4, create local-settings.json next to local-settings.example.json in the extension folder; preserve an existing file and browser storage settings. Set the exact connector display name and the person's own project URL. Prepare the project instructions from the extension popup. The default policy grants reads only in the workspace, not in the server checkout. Copy the public docs/FLEET-PROTOCOL.md into the workspace as FLEET-PROTOCOL.md (do not overwrite an existing different file); verify the copy and put its workspace path into the project instructions. Do not broaden the policy just to make the installation docs readable remotely.
- **Verify:** The popup recognizes the local server, discovers Demo, and has the correct connector and project. Only enable Fleet Tabs for the authorized smoke task; leave it off if remote policy_info did not succeed. If settings are stale, correct the saved popup configuration and reload the extension in the same profile. Do not automate account sign-in or human verification.

## 5. First project and smoke task

- **Do:** The setup script already registers Demo; do not insert a second project into the database. Use the dedicated workspace, not the server checkout. For a new empty smoke workspace, initialize a local Git repository if needed; no remote or push is necessary. Create smoke.py with VALUE = 0 and a new unused task file under ToDo/tz in the workspace; for an empty registry use TZ-1-smoke.md. The task is deliberately local and harmless. Read [fleet protocol](docs/FLEET-PROTOCOL.md) first. Use this spec:

```yaml
---
tz: TZ-1
owns: [smoke.py]
needs: []
acceptance: [python -m py_compile smoke.py]
---
```

Append a heading and body: change VALUE to 1 without editing any other file; report the change and compile result. Run acceptance locally before registration to verify prerequisites (not a substitute for worker acceptance). Preserve an existing task or file; on reruns inspect the prior result rather than creating duplicate work.

- **Do:** From the install directory, register exactly the selected TZ:
```powershell
.\fleet.cmd bootstrap Demo "$Workspace" TZ-1
.\fleet.cmd
.\fleet.cmd report Demo
.\fleet.cmd active Demo
```
- **Verify:** The bootstrap exit code is zero and the registry has the expected task/project. The bare fleet.cmd displays status (there is no status subcommand in this CLI). Enable only the Demo project in Fleet Tabs. Observe the task being taken; the worker uses fleet_join/take, writes within owns, executes acceptance and submits with its claim ID. A separate reviewer runs acceptance and issues its verdict. Do not grant platform write access or perform the worker/reviewer actions yourself merely to manufacture a pass.
- **Verify:** Run .\fleet.cmd result Demo/TZ-1 and inspect the actual write, acceptance exit code and independent accept verdict. Check smoke.py has VALUE = 1. If ready but unassigned, check extension, connector and project; if taken but blocked, inspect .\fleet.cmd active Demo and the recorded reason; if rejected, use the review evidence. If remote access is unavailable, state that the remote smoke test is unverified. Keep commit disabled for this demo.

## 6. Coding-agent integration (optional, explicit HUMAN approval)

- **Do:** For Claude Code, copy [claude/agents/fleet-quick.md](claude/agents/fleet-quick.md) to ~/.claude/agents/ without overwriting an unrelated existing definition; substitute the installation path locally. Add one clearly delimited block to ~/.claude/CLAUDE.md (merge or replace only your own block). For Codex use the user's global AGENTS.md, following that client's documented location, with the same block. Back up changed existing files.
- **Suggested block:** "For authorized fleet projects, use the dev-mcp installation at <install>. Read docs/CLAUDE-CODE-FLEET.md and docs/FLEET-PROTOCOL.md there. Check fleet.cmd active before editing; respect claims and scope. Use the normal queue for durable tasks with acceptance and independent review, and quick agents for small scoped tasks under the same authorization: by default hand a self-contained piece of work (analyse, research, write or fix files, run checks) to the `fleet-quick` subagent instead of doing it yourself, then check its diff and run the checks yourself. Inspect fleet.cmd quick --help and use the explicit project and file scope for every quick job; track results through the CLI. Never delegate credentials or privileged system changes. Close unused quick jobs."
- **Verify:** Read back exactly one block, and verify the client recognizes the installed agent definition in a new session. If not, report the client-specific loading issue. Do not grant unconditional tool permissions or modify unrelated global settings.

## 7. Optional features

- **Nightly review:** Full mode runs the nightly scheduler for explicitly selected projects in fleet-projects.toml. Under [nightly_review], projects is the code-review list, gap_projects the product-gap list, docs_projects the documentation list. The code review starts at 02:00, gaps at 03:00, docs at 04:00, with admission until 05:00 in the configured time zone. Review agents are read-only; configured gap/docs runs may create candidate tasks. Set DEV_MCP_NIGHTLY_TZ_OFFSET to a numeric UTC offset in hours (for example 2 or -5.5) if you do not want the machine's local time; the default is local time. Reports go to DEV_MCP_NIGHTLY_REPORTS if set, otherwise data/nightly in a fresh standalone installation (an existing sibling _todo directory selects its legacy nightly subdirectory). Use an explicit absolute report directory to avoid ambiguity. Set environment overrides before starting the server; verify the selected location and time window before enabling. For a dry plan, run .venv\Scripts\python.exe tools/nightly_review.py Demo --dry. Verify the plan is for the intended project; do not turn on review for personal folders.
- **Overseer:** See [Overseer](docs/OVERSEER.md). It is optional; disabled: no decision backend is normal when no supported decision backend is configured. Do not substitute account credentials or an unapproved remote model. Leave it disabled if uncertain.
- **TickTick:** Only if requested, use ticktick-connect.cmd; the human authorizes their own OAuth account. Verify the helper completes and that the intended account is connected; never reuse sample or maintainer credentials.
- **Logon startup:** Only with explicit HUMAN approval, use the Windows Task Scheduler for the current user, not SYSTEM. The following PowerShell builds a quoted task action; $Install is the agreed absolute installation directory. Review any existing task with the same name before creating it (never force-overwrite):
```powershell
$Action = '"' + (Join-Path $Install '.venv\Scripts\python.exe') + '" "' + (Join-Path $Install 'tools\standalone.py') + '" start'
schtasks.exe /Create /TN "dev-mcp-standalone" /SC ONLOGON /TR $Action /RU "$env:USERDOMAIN\$env:USERNAME" /IT /RL LIMITED
schtasks.exe /Query /TN "dev-mcp-standalone" /XML
```
  Verify the XML action's command and arguments preserve both paths even if they contain spaces, and that it runs as the intended interactive user. If quoting is wrong, do not run the task; remove only the newly created task and keep manual startup. At the next authorized logon run doctor. If creation fails, leave manual startup available. The tunnel client has its own official startup procedure; do not embed its secret in a scheduled task. Remove only this verified task if the human requests removal.

## Troubleshooting

- Local health: .venv\Scripts\python.exe tools/standalone.py doctor; inspect logs/standalone.log. For the standalone profile, stop then start using the commands above. Do not use restart.cmd for this profile (it targets a separately configured proxy launcher).
- Extension cannot see the server: verify the loopback doctor, the dedicated browser profile, extension enabled status and local endpoint. Reload the extension after changing its configuration.
- ChatGPT lacks tools: check the official tunnel doctor, account/workspace and connector; reconnect the connector because its advertised tool list is read at connection time. Verify policy_info again. Do not expose a port as a workaround.
- No task progress: inspect .\fleet.cmd, .\fleet.cmd active Demo, .\fleet.cmd result Demo/TZ-1; report a concrete blocked state rather than repeatedly submitting the same task.

## Finish

Give the human a short report: install path, workspace, local doctor evidence, authorized remote MCP call, extension status, smoke assignment/write/acceptance/review evidence, optional features enabled, and any unresolved HUMAN actions. Do not claim remote readiness from a local check. Give them [GUIDE.md](GUIDE.md) or [GUIDE.ru.md](GUIDE.ru.md).
