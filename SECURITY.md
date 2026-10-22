# Security policy

Report a suspected vulnerability through the repository's private vulnerability
reporting feature when available. Otherwise open an issue asking for a private
contact channel, without exploit details, credentials or personal files.

## Trust boundary

This server runs with the operating system privileges of its account. It is not
a sandbox. Policy roots and denied names limit exposed file operations; they do
not isolate an interpreter or an executable from the rest of the machine. An
allowed Python or Node process can invoke other programs and access anything
its OS account can access. Use a separate account or VM for untrusted code.

Keep the local HTTP endpoint on loopback. Remote access requires authenticated
TLS transport; do not expose an unauthenticated file/process server to the
internet. Store transport credentials outside policy roots and source control.
Never include real policies, task databases, logs or local extension settings
in a public export.

Only trusted operators may configure policies, integrations and deployment
routes. Task descriptions, repository files, command output and web content
are untrusted input; passing a review does not authorize an unrelated external
effect. Limit budgets and permissions at the service and OS boundaries.

The optional browser extension operates an authenticated browser account.
Review the service's current terms and account requirements before enabling
it. Keep it disabled until the intended account, project and connector are
configured. Shared credentials do not prove that an action came from a human.

This project does not promise isolation from other programs running under the
same account, protection after host compromise, or unattended deployment to
arbitrary external services. Include the release version and a minimal
redacted reproducer in private reports.
