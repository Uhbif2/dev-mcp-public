# First installation on Windows

Use a separate checkout and a directory you own. The commands below never
replace an existing policy. Python 3.11+ and Git are required; `uv` is optional.

```powershell
powershell -NoProfile -File tools/setup.ps1 -Workspace D:\work\demo -Project Demo
.venv\Scripts\python.exe tools/standalone.py start --core-only
.venv\Scripts\python.exe tools/standalone.py doctor
```

The doctor performs real MCP initialize and `policy_info` calls. The local
endpoint is `http://127.0.0.1:8766/servers/dev/mcp`. It is loopback-only; no
firewall or system proxy is changed. Review `policy.toml` before granting
write/process access to a real project. The generated policy grants those
operations only in the chosen workspace and creates one empty fleet project.

For the full fleet, stop the core-only instance and start without that flag:

```powershell
.venv\Scripts\python.exe tools/standalone.py stop
.venv\Scripts\python.exe tools/standalone.py start
.venv\Scripts\python.exe tools/standalone.py status
```

## Connect ChatGPT

Use [Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
with the same loopback HTTP endpoint. Create your own tunnel in Platform
settings and grant its runtime key the required tunnel permissions. Associate
the tunnel with the intended ChatGPT workspace, then add the custom MCP plugin
using its Tunnel connection. Follow the current official onboarding guide and
`tunnel-client help quickstart`; the package does not contain account keys or
create account permissions on your behalf.

For an HTTP profile, configure `--mcp-server-url
http://127.0.0.1:8766/servers/dev/mcp`. Run the tunnel's doctor and keep its
client running. Verify `policy_info` from the intended ChatGPT conversation.
Tunnel authorization and browser sign-in are separate from local readiness.

An existing authenticated HTTPS gateway is also supported. Keep production
gateway credentials private; never copy another operator's credentials.

## Fleet browser and optional integrations

Install the [unpacked extension](extension/fleet-tabs/README.md), configure
your project and connector, sign in yourself and enable it only after the
remote MCP call succeeds. Add a small task under the project's `ToDo/tz`
using the [fleet protocol](docs/FLEET-PROTOCOL.md), then verify assignment,
write, acceptance, independent review and the result presented to the owner.

TickTick is optional: run `ticktick-connect.cmd` and authorize your own account
through the official MCP. Memory and model relays are optional; no shared
operator accounts are included. The minimal profile is local MCP + one
workspace + authenticated tunnel + signed-in Chrome + the extension.

## Startup, update and removal

`start`, `stop`, `status` and `doctor` control only the PID identity recorded by
this installation. Logs are in `logs/standalone.log`. To use an existing
manifest-based autostart orchestrator, explicitly opt in:

```powershell
.venv\Scripts\python.exe tools/standalone.py autostart-install --manifest D:\ops\autostart.json
.venv\Scripts\python.exe tools/standalone.py uninstall --manifest D:\ops\autostart.json
```

This updates only this installation's entry and stores a backup under `data`.
The manifest's orchestrator itself is external. Otherwise start the server and
tunnel through your chosen service manager. No startup entry is installed by
default. `uninstall` stops the instance and removes its optional manifest entry;
it preserves projects, policy, OAuth grants and task history for recovery.

Before updating, stop this instance and back up configuration/data. Review the
release diff, install dependencies, start and run doctor plus a real remote
call. Revert the checkout/dependencies and restore the backup if validation
fails. Acceptance of a clean installation additionally requires a task after
a reboot in the target environment; local doctor alone does not prove that.
