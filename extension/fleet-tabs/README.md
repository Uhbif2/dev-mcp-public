# Fleet Tabs (Chrome, Manifest V3)

The extension executes dev-mcp's decisions in fleet chat tabs: reserve work,
open a chat, continue an assigned task, recover a broken page and close an idle
interface. Worker state and task ownership come from the server.

## Install

1. Run your own dev-mcp behind an MCP proxy exposing
   `http://127.0.0.1:8766/servers/dev/mcp`. The proxy/gateway is a separate deployment.
2. Extract the release ZIP (or use this directory). In `chrome://extensions`,
   enable Developer mode and choose **Load unpacked**. Select the directory
   containing `manifest.json`.
3. Copy `local-settings.example.json` to `local-settings.json` in that directory.
   Set `projectUrl` to your own ChatGPT project's URL and `connector` to the
   exact display name of your installed dev-mcp connector. Use a browser profile
   where you are signed in to that account. The optional
   `start-fleet-chrome.cmd` uses a separate `FleetChrome` profile.
4. In the popup, copy the project instructions and paste them into your ChatGPT
   project. Supply the installation path in those instructions.
5. Reload the extension, then turn **Включено** on. Available fleet projects are
   discovered from your server; select the ones to work on.

Fresh installs start disabled, with no project list or private URLs. Settings in
`chrome.storage.local` take precedence over `local-settings.json`. Keep the latter
local; do not upload it with the extension. The popup reads the same configuration
as the background worker.

The distributed manifest uses a dedicated public extension ID, distinct from the
maintainer's private installation. It requests chatgpt.com and loopback access,
tabs, alarms, local storage, notifications, scripting and system memory.
It contains no telemetry collection endpoint. Diagnostic exports are downloaded
locally; chat addresses and fleet status are sent to your own dev-mcp server.

## Checks and limits

`npm test` runs policy, recovery, runtime, package and DOM tests. DOM tests use a
headless Chrome child with a local fixture; set `CHROME_PATH` if Chrome is elsewhere.
They do not log into ChatGPT or exercise the live website.

The extension manages labelled fleet tabs. User drafts, login screens, human
verification, model limits and anti-abuse banners prevent automated sending.
Browser interface changes can require adapter updates. Use it in accordance
with the service terms applicable to your account.

This is an unpacked-extension release, not a Chrome Web Store listing.
