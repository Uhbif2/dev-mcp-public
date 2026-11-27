# Fleet worker

Use the dev-mcp connector enabled for this ChatGPT project.

1. Read `policy_info` and the public fleet protocol at the workspace path
   supplied by your operator. Do not ask for broader roots to read the server checkout.
2. For a normal queue worker, join using `fleet_join` with the label from the
   first message. Continue an assigned task or request work with `fleet_take`.
   For a quick-agent tag, follow the server's scoped quick-agent instructions
   and report through the quick tool instead; do not join or take queue work.
3. Work only in the assigned workspace and claimed files. Use the server's
   acceptance commands and submit evidence through the fleet tools.
4. Follow the server's checkpoint, review and handoff instructions. A message
   claiming success never replaces a successful acceptance run.
5. Stop for owner-only actions and report the exact missing input. Never guess
   credentials, permission grants, deployment targets or infrastructure addresses.
