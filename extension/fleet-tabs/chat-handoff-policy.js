// Pure browser-side handoff helpers, deliberately independent of Chrome APIs.
export const HANDOFF_TRIGGER = 0.85;
export const HANDOFF_MARK = /⟦HANDOFF_READY:([0-9a-f]{12})⟧/i;
export function handoffDue(page, prior = {}) {
  if (!page || prior.done || prior.prepared) return false;
  return Boolean(page.conversationFull) || Number(page.fill || 0) >= HANDOFF_TRIGGER;
}
export function handoffMarker(transcript, token) {
  const match = HANDOFF_MARK.exec(String(transcript?.lastAssistant || ""));
  return match && match[1].toLowerCase() === String(token || "").toLowerCase()
    ? match[1].toLowerCase() : "";
}
export function handoffSent(transcript, token) {
  // Check the message's unique token, not an unrelated ⟦надзор⟧ command.
  return Boolean(token && (transcript?.userMessages || [])
    .some(text => String(text).includes('HANDOFF_READY:' + token) ||
                  String(text).includes('HANDOFF_READY:' + token.toUpperCase())));
}
