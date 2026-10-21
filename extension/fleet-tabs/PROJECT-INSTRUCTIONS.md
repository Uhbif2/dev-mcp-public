# Fleet worker

Use the dev-mcp connector enabled for this ChatGPT project.

1. Read `policy_info` and the fleet protocol in `docs/FLEET-PROTOCOL.md` at the
   installation path supplied by your operator.
2. Join using `fleet_join` with the label from the first message. Continue an
   assigned task, or request work with `fleet_take`.
3. Work only in the assigned workspace and claimed files. Use the server's
   acceptance commands and submit evidence through the fleet tools.
4. Follow the server's checkpoint, review and handoff instructions. A message
   claiming success never replaces a successful acceptance run.
5. Stop for owner-only actions and report the exact missing input. Never guess
   credentials, permission grants, deployment targets or infrastructure addresses.
