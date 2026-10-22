# Changelog

## 0.1.0 / Fleet Tabs 0.19.6 — 2026-10-22

First complete experimental public release of the policy-bounded Windows MCP
server, fleet registry and Chrome extension.

- The public tree contains the core, fleet, tests, operational documentation and
  an unpacked Chrome extension. Existing projects, tasks, history, production
  policies, credentials and host addresses are excluded.
- Machine-specific paths and optional host integrations are configured through
  `machine.toml`. A fresh checkout has no live listener guard or project publisher.
- Extension URLs, connector names and optional initial projects belong to
  `local-settings.json`, with saved browser settings taking precedence. New
  installs start disabled and discover projects from their own fleet server.
- The public extension uses its own stable ID. Generic project instructions and
  configuration examples are included in both the source and extension ZIP.
- The lock file now includes the declared `psutil` dependency, so the documented
  frozen installation is complete.
- Release archives include SHA-256 sums. The public source inventory records a
  SHA-256 for each file. Secret scanning keeps the default Gitleaks rules, with
  narrow exceptions for checksums and the extension's public RSA key.

The browser adapter and host integrations remain experimental. The extension
is distributed unpacked; this release is not a Chrome Web Store submission.
Gateway hosting, credentials and account setup are supplied by each operator.
## 0.1.1 — 2026-10-22

- Keep reading owner card edits and reopen events after project binding;
  explicitly filter active cards. TickTick choices record an operational request;
  privileged execution requires separate attributable owner authorization.
- `restart.py --help` and invalid arguments cannot restart services.
- Make UTF-8 test fixtures independent of the Windows system code page.

- Preserve recorded process argument boundaries during independent acceptance.
- Respect TickTick rate limits, report sync request counts and bind approvals
  to the plan version originally presented by the question.
- Support explicit question deadlines and existing duration tags; owner section
  moves update the intent and moving back to the inbox pauses it.
- Add loopback HTTP, core-only mode, standalone setup/doctor and opt-in
  manifest startup management without machine-wide settings changes.
- Add Windows CI, production test markers, security guidance and the missing
  TickTick connection helper.

