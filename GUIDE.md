# Using dev-mcp

[Русский](GUIDE.ru.md) · [Installation](AGENT-SETUP.md)

dev-mcp is a Windows MCP server and a fleet of coding agents in your own ChatGPT browser tabs. The normal queue reserves files, records acceptance checks and sends work to an independent reviewer. Quick agents handle small scoped jobs without the queue. Optional nightly review looks for code, product and documentation issues; the optional overseer helps track progress in selected conversations, when a decision backend is available.

Tell your local coding agent: “Put this task into the fleet with acceptance criteria and independent review” or “Give this small, scoped task to a quick agent and verify the result.” You do not need to operate the queue by hand. Keep tasks in projects you control and let the agent choose the correct path. The Fleet Tabs popup, its badge and the chat tabs show activity. From the installation directory, run fleet.cmd for status, or fleet.cmd result Demo/TZ-1 for a specific task.

If something breaks, ask your agent to run these three commands from the installation directory:
```powershell
.venv\Scripts\python.exe tools/standalone.py doctor
.\fleet.cmd
.venv\Scripts\python.exe tools/fleet_chrome.py status
```
If local health is good but ChatGPT cannot see the tools, reconnect the authorized connector; see [AGENT-SETUP.md](AGENT-SETUP.md). The server log is logs/standalone.log.

Never grant broad access in policy.toml, expose an unauthenticated public tunnel, or put passwords/tokens in chat. Write + execution is code execution with your OS user's privileges; use dedicated workspaces and trusted agents. Stop for unexpected account prompts or security warnings. [Security](SECURITY.md).
