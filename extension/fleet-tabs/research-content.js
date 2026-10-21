// Dedicated Deep Research transport; never uses a normal-chat response as a
// completed research report. Isolated from the fleet agent send/lock protocol.
(() => {
  "use strict";
  if (globalThis.__fleetResearchAdapterV1) return;
  globalThis.__fleetResearchAdapterV1 = true;

  const composerSelectors = [
    "[data-testid='prompt-textarea']", "#prompt-textarea",
    "main [contenteditable='true'][role='textbox']",
    "main [contenteditable='plaintext-only'][role='textbox']",
    "main .ProseMirror[contenteditable='true']",
    "textarea[name='prompt-textarea']", "form textarea",
  ];
  const stopSelector = "[data-testid='stop-button'],button[aria-label*='Stop' i],button[aria-label*='Останов' i]";
  const sendSelector = "#composer-submit-button,[data-testid='send-button'],button[aria-label*='Send' i],button[aria-label*='Отправ' i]";
  const assistantSelector = "[data-message-author-role='assistant'],[data-turn='assistant']";
  const unavailable = /(reached (your|the) (deep )?research limit|deep research is unavailable|deep research limit reached|достигнут лимит.*исследован|глубокое исследование недоступно|verify you are human|подтвердите, что вы человек|log in again|войдите снова)/i;
  const busy = /(researching|searching the web|analyzing sources|исследование выполняется|изучение источников|поиск в интернете)/i;
  const startPlan = /^(?:start research|begin research|run research|начать исследование|запустить исследование)$/i;
  const stable = new Map();
  const started = new Set();
  const approved = new Set();
  let acting = false;

  function visible(el) {
    return Boolean(el && el.getClientRects?.().length &&
      getComputedStyle(el).visibility !== "hidden" &&
      el.getAttribute("aria-hidden") !== "true");
  }

  function detectBlocker() {
    const text = (document.querySelector("[role='alertdialog'],[role='dialog'],main")?.innerText || "").slice(-5000);
    return unavailable.test(text) ? "research quota, login or verification unavailable" : "";
  }

  function findComposer() {
    for (const selector of composerSelectors) {
      const element = [...document.querySelectorAll(selector)].find(visible);
      if (element) return element;
    }
    return null;
  }

  function readComposer(el) {
    return String(typeof el.value === "string" ? el.value : el.innerText || el.textContent || "").trim();
  }

  function typeQuestion(el, question) {
    el.focus();
    if (typeof el.value === "string") {
      const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value")?.set;
      if (setter) setter.call(el, question);
      else el.value = question;
      el.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      document.execCommand("selectAll", false);
      document.execCommand("insertText", false, question);
      el.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: question }));
    }
    return readComposer(el) === question;
  }

  async function start(request) {
    if (acting) return { status: "pending", reason: "previous action in progress" };
    acting = true;
    try {
      if (!/^r-[0-9a-f]{24}$/.test(String(request.id || "")) ||
          typeof request.question !== "string" ||
          !request.question.trim() || request.question.length > 8000) {
        return { status: "unavailable", reason: "invalid request identity or question" };
      }
      if (started.has(request.id)) return { status: "pending" };
      if (sessionStorage.getItem("fleet-research:" + request.id) === "sent") {
        started.add(request.id);
        return { status: "pending" };
      }
      const blocked = detectBlocker();
      if (blocked) return { status: "unavailable", reason: blocked };
      if (!globalThis.FleetChatModes?.ensureMode) {
        return { status: "unavailable", reason: "mode adapter unavailable" };
      }
      // Only a verified, accessible Research control is valid evidence.
      const mode = await globalThis.FleetChatModes.ensureMode("deep-research", { root: document });
      if (!mode.ok || mode.effective !== "deep-research") {
        return { status: "unavailable", reason: "Deep Research mode not verified: " + String(mode.detail || "").slice(0, 140) };
      }
      const field = findComposer();
      if (!field) return { status: "unavailable", reason: "ChatGPT composer unavailable" };
      if (readComposer(field)) return { status: "unavailable", reason: "ChatGPT composer contains another draft" };
      if (!typeQuestion(field, request.question)) {
        return { status: "unavailable", reason: "could not type question into composer" };
      }
      const send = [...document.querySelectorAll(sendSelector)].find((el) =>
        visible(el) && !el.disabled && el.getAttribute("aria-disabled") !== "true");
      if (!send) return { status: "unavailable", reason: "send control unavailable" };
      // Mark before click: a lost browser response must not submit twice.
      sessionStorage.setItem("fleet-research:" + request.id, "sent");
      send.click();
      started.add(request.id);
      return { status: "pending" };
    } catch (error) {
      return { status: "unavailable", reason: "research start exception: " + String(error).slice(0, 160) };
    } finally {
      acting = false;
    }
  }

  function poll(request) {
    if (!started.has(request.id) &&
        sessionStorage.getItem("fleet-research:" + request.id) === "sent") {
      started.add(request.id);
    }
    if (!started.has(request.id)) return { status: "unavailable", reason: "research tab lost submission state" };
    const blocker = detectBlocker();
    if (blocker) return { status: "unavailable", reason: blocker };
    // A proposed plan may need explicit approval before execution.
    const planButtons = [...document.querySelectorAll("main button,[role='dialog'] button")]
      .filter((el) => visible(el) && !el.disabled &&
        startPlan.test((el.getAttribute("aria-label") || el.innerText || el.textContent || "").trim()));
    if (!approved.has(request.id) && planButtons.length === 1) {
      approved.add(request.id);
      planButtons[0].click();
      return { status: "pending" };
    }
    if (planButtons.length > 1) {
      return { status: "unavailable", reason: "ambiguous research plan confirmation" };
    }
    if (document.querySelector(stopSelector)) return { status: "pending" };
    const turns = [...document.querySelectorAll(assistantSelector)].filter(visible);
    // The report may open in a fullscreen viewer, not in the chat transcript.
    const reportPanels = [...document.querySelectorAll(
      "[data-testid*='research-report'],[data-testid*='report-viewer'],[role='dialog']"
    )].filter((node) => visible(node) &&
      /(?:sources used|использованные источники|activity history|история действий)/i.test(
        (node.innerText || "").slice(-30000)));
    const last = reportPanels.length === 1 ? reportPanels[0] : turns[turns.length - 1];
    if (!last) return { status: "pending" };
    const text = (last.innerText || "").trim();
    // The plan/progress UI is not the final report. A distinct report with
    // references and section headings is required before collecting content.
    const sources = [...last.querySelectorAll("a[href^='https://'],a[href^='http://']")]
      .filter((a) => /^https?:\/\//.test(a.href) && !a.href.includes("chatgpt.com"));
    const headings = last.querySelectorAll("h1,h2,h3,[role='heading']");
    if (text.length < 700 || sources.length < 2 || headings.length < 1 || busy.test(text.slice(-200))) {
      return { status: "pending" };
    }
    const links = [...new Map(sources.map((a) =>
      [a.href, "- " + (a.innerText || a.textContent || "source").trim().slice(0, 120) + ": " + a.href])).values()];
    const report = (text + "\n\n## Source links\n" + links.join("\n")).slice(0, 200000);
    const previous = stable.get(request.id);
    const now = Date.now();
    if (previous?.report === report && now - previous.at >= 15000) {
      return { status: "complete", report };
    }
    if (!previous || previous.report !== report) stable.set(request.id, { report, at: now });
    return { status: "pending" };
  }

  chrome.runtime.onMessage.addListener((message, _sender, reply) => {
    if (message?.type === "research-start") {
      start(message.request).then(reply).catch((err) =>
        reply({ status: "unavailable", reason: String(err).slice(0, 160) }));
      return true;
    }
    if (message?.type === "research-poll") {
      reply(poll(message.request));
      return false;
    }
    return false;
  });
})();
