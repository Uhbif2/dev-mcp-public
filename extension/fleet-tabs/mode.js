// Versioned mode UI adapter: only real, accessible controls are evidence.
// Loaded as a classic isolated content script; never evals page JavaScript.
(() => {
  "use strict";
  const VERSION = "fleet-chat-mode-ui/v1";
  const MODES = new Set(["default", "thinking", "deep-research"]);
  const LABELS = Object.freeze({
    default: /^(?:instant|default|обычный|мгновенный)$/i,
    thinking: /^(?:think|thinking|думать|размышлять)$/i,
    "deep-research": /^(?:deep research|глубокое исследование)$/i,
    tools: /^(?:tools|инструменты)$/i,
  });
  const CONTROL = "button,[role='button'],[role='menuitem'],[role='menuitemradio'],[role='option'],[role='radio']";
  // Labels in conversation content, editable prompts and navigation are not
  // browser model controls. A fake "Think" button there must never be evidence.
  const EXCLUDED = "[data-message-author-role],[data-turn],article,#fleet-lock,pre,code,nav,aside,[role='navigation'],[contenteditable='true'],[contenteditable='plaintext-only']";

  function normalizeMode(mode) {
    if (typeof mode !== "string" || !MODES.has(mode)) {
      throw new TypeError("unsupported ChatGPT mode");
    }
    return mode;
  }

  function normalizeLabel(value) {
    return String(value || "").trim().replace(/\s+/g, " ").slice(0, 100);
  }

  function eligible(element) {
    if (typeof element?.getAttribute !== "function" || element.closest?.(EXCLUDED)) return false;
    if (element.hasAttribute?.("disabled") || element.getAttribute("aria-disabled") === "true") return false;
    if (element.getAttribute("aria-hidden") === "true") return false;
    if (!element.getClientRects?.().length) return false;
    if (typeof getComputedStyle === "function" && getComputedStyle(element).visibility === "hidden") return false;
    return true;
  }

  function label(element) {
    return normalizeLabel(element.getAttribute("aria-label") || element.textContent);
  }

  function selected(element) {
    return ["aria-pressed", "aria-checked", "aria-selected"].some(
      (attribute) => element.getAttribute(attribute) === "true",
    ) || ["on", "checked", "active"].includes(element.getAttribute("data-state"));
  }

  function selectable(element) {
    return ["aria-pressed", "aria-checked", "aria-selected"].some(
      (attribute) => ["true", "false"].includes(element.getAttribute(attribute)),
    ) || ["on", "off", "checked", "unchecked", "active", "inactive"].includes(element.getAttribute("data-state"));
  }

  function scan(root) {
    const controls = [...root.querySelectorAll(CONTROL)].filter(eligible);
    return Object.fromEntries(Object.keys(LABELS).map((name) =>
      [name, controls.filter((element) => LABELS[name].test(label(element)))]));
  }

  function probe(root = document) {
    const found = scan(root);
    const verified = Object.fromEntries([...MODES].map((mode) => [
      mode, found[mode].length === 1 && selectable(found[mode][0]) && selected(found[mode][0]),
    ]));
    const available = Object.fromEntries([...MODES].map((mode) => [
      mode, found[mode].length === 1 && (mode !== "default" || selectable(found[mode][0])),
    ]));
    const active = Object.keys(verified).filter((key) => verified[key]);
    return {
      adapterVersion: VERSION, available,
      active: active.length === 1 ? active[0] : null,
      ambiguous: active.length > 1 || Object.keys(found).some((key) => found[key].length > 1),
      observedLabels: Object.entries(found).filter(([, elements]) => elements.length)
        .map(([name, elements]) => name + ":" + elements.length).sort(),
    };
  }

  function result(ok, desired, effective, evidence, detail) {
    return { ok, wait: !ok, requested: desired, effective, evidence, detail };
  }

  function resolveMode(requested, stage, capabilities) {
    const wanted = normalizeMode(requested);
    return wanted === "default" && stage === "понято" &&
      capabilities?.available?.["deep-research"] === true ? "deep-research" : wanted;
  }

  async function ensureMode(requested, { stage = null, root = document, sleep = null } = {}) {
    let wanted;
    try { wanted = normalizeMode(requested); }
    catch { return result(false, requested, null, { adapterVersion: VERSION }, "invalid mode metadata"); }
    const pause = sleep || ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    let before = probe(root);
    // Deep Research can be an item inside the closed Tools menu. Probe that
    // menu explicitly for the single "понято" preference, not for other
    // stages, and never treat the label Tools itself as proof of Research.
    let openedMenuForProbe = false;
    if (wanted === "default" && stage === "понято" &&
        !before.available["deep-research"] && !before.ambiguous) {
      const tools = scan(root).tools;
      if (tools.length === 1) {
        tools[0].click();
        openedMenuForProbe = true;
        await pause(120);
        before = probe(root);
      }
    }
    const effective = resolveMode(wanted, stage, before);
    if (before.ambiguous) return result(false, wanted, effective, before, "ambiguous mode controls");
    if (before.active === effective) {
      if (openedMenuForProbe && effective === "default") {
        const tools = scan(root).tools;
        if (tools.length === 1) tools[0].click();
      }
      return result(true, wanted, effective, before, "already active");
    }
    let found = scan(root);
    if (effective === "deep-research" && !before.available[effective]) {
      if (found.tools.length !== 1) {
        return result(false, wanted, effective, before, "deep-research menu is not available");
      }
      found.tools[0].click();
      await pause(120);
      found = scan(root);
    }
    if (found[effective].length !== 1) {
      return result(false, wanted, effective, probe(root), "mode control unavailable");
    }
    found[effective][0].click();
    for (let i = 0; i < 4; i += 1) {
      await pause(150);
      const after = probe(root);
      if (!after.ambiguous && after.active === effective) {
        return result(true, wanted, effective, after, "active mode verified");
      }
    }
    return result(false, wanted, effective, probe(root), "mode switch not verified; wait");
  }

  globalThis.FleetChatModes = Object.freeze({
    VERSION, normalizeMode, resolveMode, probe, ensureMode,
  });
})();
