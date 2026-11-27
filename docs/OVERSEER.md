# Optional conversation overseer

The overseer observes completed turns in an explicitly selected owner conversation. It is not a worker, reviewer or source of permissions. It evaluates progress toward the owner's actual goal, distinguishes waiting and owner-only obstacles, and can propose a bounded next step. It must not repeat an unchanged blocked action or treat a continuation message as a new goal.

A supported decision backend is required. With none configured, disabled: no decision backend is normal. Do not provide another person's credentials or assume a local model is installed. Keep the feature off unless the operator has authorized the backend and the handling of conversation content.

The operator can inspect fleet.cmd overseer --help. Use observation-only (shadow) before authorizing any sending; turn it off when no longer needed. Review the actual outcome: a decision is not evidence that a browser message was delivered or that a task succeeded. New owner input supersedes pending decisions. Uncertain classification, unavailable backend and persistent blockers should result in waiting or escalation, not repeated activity. Respect account limits and any human-verification prompts.

Do not use the overseer to bypass access controls or extend a worker's scope. Conversation exports, journals and decision records are local operator data, not public examples. See [SECURITY.md](../SECURITY.md).
