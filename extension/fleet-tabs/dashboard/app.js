/* Owner dashboard. The extension never reads memory_service, Obsidian, raw drafts,
 * private files, or worker logs. All data must come from a narrow server-side
 * projection. No untrusted string is ever rendered as HTML.
 */
import { callTool, DEFAULT_MCP_URL, reportExtension } from "../fleet.js";
import { markOwnerPanelOpened } from "../owner-notices.js";

const $ = (id) => document.getElementById(id);
const refs = {
  summary: $("summary"), tree: $("tree"), notice: $("notice"),
  ownerActions: $("owner-actions"), humanIntents: $("human-intents"),
  humanDone: $("human-done"), fleetLine: $("fleet-line"),
  connection: $("connection"), refresh: $("refresh"),
  dialog: $("taste-dialog"), tasteOptions: $("taste-options"),
  tasteActions: $("taste-actions"), tasteStatus: $("taste-status"),
  login: $("owner-login"), password: $("owner-password"),
};
const POLL_MS = 8000;
const MAX_IMAGE_BYTES = 2 * 1024 * 1024;
const IMAGE_MIMES = new Set(["image/png", "image/webp", "image/jpeg"]);

function node(tag, label = null, className = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (label !== null) element.textContent = String(label);
  return element;
}
function clear(element) { element.replaceChildren(); }
function value(text) { return typeof text === "string" ? text : ""; }
function ago(timestamp) {
  const elapsed = Date.now() - Date.parse(value(timestamp));
  if (!Number.isFinite(elapsed) || elapsed < 0) return "время неизвестно";
  const minutes = Math.floor(elapsed / 60000);
  if (minutes < 1) return "только что";
  if (minutes < 60) return minutes + " мин назад";
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return hours + " ч назад";
  return Math.floor(hours / 24) + " дн назад";
}
function localLink(uri, label) {
  if (!/^file:\/\/\/[A-Za-z]:\//.test(value(uri))) return node("span", "Путь недоступен", "meta");
  const link = node("a", label);
  link.href = uri;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}
function renderHuman(human = {}) {
  for (const region of [refs.ownerActions, refs.humanIntents, refs.humanDone]) clear(region);
  const actions = Array.isArray(human?.actions) ? human.actions : [];
  if (!actions.length) refs.ownerActions.append(node("p", "Сейчас нет запросов на ваше решение", "human-empty"));
  for (const alert of human?.fleet?.system_alerts || []) {
    const card = node("article",null,"human-card action-card");
    card.append(node("h3","Системное исключение"),node("p",value(alert.text),"human-main"));
    refs.ownerActions.append(card);
  }
  for (const item of actions) {
    const card = node("article", null, "human-card action-card");
    card.append(node("h3", value(item.title) || "Нужно ваше участие"));
    card.append(node("p", value(item.action), "human-main"));
    card.append(node("p", "По умолчанию: " + value(item.default), "meta"));
    card.append(node("p", "Почему требуется: " + value(item.reason), "meta"));
    const button = node("button", "Сделано", "primary");
    button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const ack = await transport.done(value(item.project), value(item.action_id),
          value(lastSnapshot?.revision));
        if (ack.status !== "ready") throw Error("confirmation pending");
        await refresh();
      } catch (_) {
        refs.notice.hidden = false;
        refs.notice.textContent = "Не удалось подтвердить действие. Повторите после восстановления связи.";
      } finally {
        button.disabled = false;
      }
    });
    card.append(button);
    refs.ownerActions.append(card);
  }
  const projects = Array.isArray(human?.projects) ? human.projects : [];
  if (!projects.length) refs.humanIntents.append(node("p", "Активных интенций пока нет", "human-empty"));
  for (const intent of projects) {
    const card = node("article", null, "human-card");
    card.append(node("h3", value(intent.title) || "Интенция"));
    const stages = node("ol", null, "human-stages");
    (Array.isArray(intent.stages) ? intent.stages : []).forEach((stage) => {
      const element = node("li", value(stage.label), stage.done ? "stage-complete" : "");
      if (value(stage.label) === value(intent.current)) element.classList.add("stage-current");
      stages.append(element);
    });
    card.append(stages);
    card.append(node("p", "Сейчас: " + value(intent.current) + ". " +
                value(intent.description), "human-main"));
    card.append(node("p", "Идёт " + ago(intent.started), "meta"));
    const links = node("div", null, "human-links");
    links.append(localLink(intent.folder, "Открыть папку"),
                 localLink(intent.document, "Документ этапа"));
    card.append(links);
    refs.humanIntents.append(card);
  }
  const done = Array.isArray(human?.done) ? human.done : [];
  if (!done.length) refs.humanDone.append(node("p", "Переданных результатов пока нет", "human-empty"));
  for (const item of done) {
    const card = node("article", null, "human-card");
    card.append(node("h3", value(item.title) || "Результат"));
    card.append(node("p", value(item.text) || "Готово", "human-main"));
    refs.humanDone.append(card);
  }
  const fleet = human?.fleet || {};
  const release = fleet.release;
  const releaseStatus = release
    ? (release.success ? "успешно, " : "нет подтверждения, ") + ago(release.at)
    : "пока нет";
  const agents = Number.isSafeInteger(fleet.agents) ? fleet.agents : 0;
  const queue = Number.isSafeInteger(fleet.queue) ? fleet.queue : 0;
  const overloads = Number.isSafeInteger(fleet.overloads) ? fleet.overloads : 0;
  const states = fleet.agent_states;
  const agentLine = states && ["working","thinking","silent","waiting"].every((key) => Number.isSafeInteger(states[key]))
    ? `MCP≤2мин ${states.working} · генерация UI ${states.thinking} · не подтверждены ${states.silent} · ждут ${states.waiting}`
    : "Работают " + agents + " агентов";
  refs.fleetLine.textContent = agentLine + " · В очереди " + queue +
    " задач · Выкатка: " + releaseStatus + " · Перегрузки за сутки: " + overloads +
    (Number.isSafeInteger(fleet.pending_notices) && fleet.pending_notices > 0
      ? " · Ожидают доставки уведомления: " + fleet.pending_notices : "");
}

function countStages(intents) {
  const count = { total: 0, applied: 0, proven: 0, delivered: 0 };
  const visit = (item) => {
    count.total += 1;
    for (const stage of ["applied", "proven", "delivered"])
      if (item.stage?.[stage] === true) count[stage] += 1;
    (item.children || []).forEach(visit);
  };
  intents.forEach((intent) => (intent.results || []).forEach(visit));
  return count;
}
function renderInterventions(metrics) {
  const section = node("section", null, "node");
  section.style.gridColumn = "1 / -1";
  section.append(node("h3", "Ручные вмешательства · последние 7 дней"));
  if (!metrics || metrics.available === false) {
    section.append(node("p", "Учёт вмешательств пока недоступен", "meta"));
    return section;
  }
  const number = (n) => Number.isSafeInteger(n) && n >= 0 ? n : null;
  const today = number(metrics.today);
  const yesterday = number(metrics.yesterday);
  const goal = number(metrics.goal);
  if (today === null || yesterday === null || goal === null ||
      typeof metrics.rising !== "boolean" ||
      !metrics.by_day || typeof metrics.by_day !== "object" ||
      Array.isArray(metrics.by_day)) {
    section.append(node("p", "Сводка вмешательств повреждена", "meta"));
    return section;
  }
  section.append(node("div",
    `Сегодня: ${today} · Вчера: ${yesterday} · Цель: ${goal} · ${metrics.rising ? "Рост вмешательств" : "Без роста"}`,
    "work"));
  const days = Object.entries(metrics.by_day)
    .filter(([day, classes]) => /^\d{4}-\d{2}-\d{2}$/.test(day) &&
            classes && typeof classes === "object" && !Array.isArray(classes))
    .sort(([a], [b]) => a.localeCompare(b)).slice(-7);
  for (const [day, classes] of days) {
    const rows = Object.entries(classes)
      .filter(([label, count]) => typeof label === "string" && number(count) !== null)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([label, count]) => `${label}: ${count}`);
    section.append(node("div", `${day} · ${rows.length ? rows.join(" · ") : "0 вмешательств"}`, "work"));
  }
  return section;
}

function renderSummary(intents, interventions = null) {
  clear(refs.summary);
  const totals = countStages(intents);
  for (const [label, number] of [
    ["Результатов", totals.total], ["Применено", totals.applied],
    ["Доказано", totals.proven], ["Доставлено", totals.delivered],
  ]) {
    const box = node("div", null, "stat");
    box.append(node("strong", number), node("span", label));
    refs.summary.append(box);
  }
  refs.summary.append(renderInterventions(interventions));
}
function stageBadge(label, passed) {
  return node("span", passed ? label + " ✓" : label + " —",
              "stage" + (passed ? " done" : ""));
}
function renderResult(item) {
  const box = node("article", null, "node");
  box.append(node("h3", value(item.title) || "Результат без названия"));
  box.append(node("div", `Версия: ${value(item.subject_version)} · ID: ${value(item.id)}`, "meta"));
  const badges = node("div", null, "badge-row");
  badges.append(stageBadge("Применено", item.stage?.applied === true),
                stageBadge("Доказано", item.stage?.proven === true),
                stageBadge("Доставлено", item.stage?.delivered === true));
  badges.append(node("span", value(item.state), "stage state"));
  box.append(badges);

  const work = Array.isArray(item.work) ? item.work : [];
  if (work.length) {
    box.append(node("div", "Текущая работа", "meta"));
    for (const w of work)
      box.append(node("div", `${value(w.tz)} · ${value(w.state)}`, "work"));
  }
  const proofs = Array.isArray(item.evidence) ? item.evidence : [];
  if (proofs.length) {
    box.append(node("div", "Доказательства текущей версии", "meta"));
    for (const proof of proofs) {
      box.append(node("div",
        `${value(proof.kind)} · ${value(proof.verdict)} · версия ${value(proof.subject_version)} · среда ${value(proof.env_version)}`,
        "work"));
    }
  }
  for (const wait of item.waiting || [])
    box.append(node("div", value(wait.reason) || "Причина ожидания неизвестна", "wait"));
  if ((item.waiting || []).some((wait) => wait.cause_code === "owner_taste")) {
    const actions = node("div", null, "actions");
    const open = node("button", "Посмотреть варианты");
    open.type = "button";
    open.addEventListener("click", () => showGallery(value(item.id)));
    actions.append(open);
    box.append(actions);
  }
  if (Array.isArray(item.children) && item.children.length) {
    const children = node("div", null, "children");
    item.children.forEach((child) => children.append(renderResult(child)));
    box.append(children);
  }
  return box;
}
function renderOverview(payload) {
  renderHuman(payload.human);
  const intents = Array.isArray(payload?.intents) ? payload.intents : [];
  renderSummary(intents, payload.interventions);
  clear(refs.tree);
  if (payload.status === "empty" || !intents.length) {
    refs.tree.append(node("p", value(payload.message) || "Результатов пока нет", "empty"));
  } else {
    for (const intent of intents) {
      const section = node("section", null, "intent");
      section.append(node("div", "Интенция · " + value(intent.id), "intent-title"));
      for (const result of intent.results || []) section.append(renderResult(result));
      refs.tree.append(section);
    }
  }
  if (payload.status === "error") {
    refs.notice.hidden = false;
    refs.notice.textContent = value(payload.message) || "Снимок содержит ошибки";
  } else {
    refs.notice.hidden = true;
    refs.notice.textContent = "";
  }
}
function parseJSON(text) {
  const data = JSON.parse(text);
  if (!data || typeof data !== "object" || Array.isArray(data)) throw Error("invalid dashboard payload");
  return data;
}

// Bearer capability stays in page memory only; a public worker ID is not auth.
let sessionToken = "";
function ownerSession() {
  if (!/^dash-[A-Za-z0-9_-]{43}$/.test(sessionToken)) throw Error("owner login required");
  return sessionToken;
}

// All data tools require an authenticated dashboard session.
export const transport = {
  async login(password) {
    return parseJSON(await callTool(DEFAULT_MCP_URL, "owner_dashboard_login", { password }));
  },
  async overview() {
    return parseJSON(await callTool(DEFAULT_MCP_URL, "owner_dashboard_overview",
      { session_token: ownerSession(), project: $("project-filter").value.trim() }));
  },
  async done(project, action_id, expected_revision) {
    return parseJSON(await callTool(DEFAULT_MCP_URL, "owner_dashboard_done",
      { session_token: ownerSession(), project, action_id, expected_revision }));
  },
  async gallery(outcome_id) {
    return parseJSON(await callTool(DEFAULT_MCP_URL, "owner_dashboard_gallery",
      { outcome_id, session_token: ownerSession() }));
  },
  async choose(outcome_id, option_id, expected_revision) {
    return parseJSON(await callTool(DEFAULT_MCP_URL, "owner_dashboard_choose",
      { outcome_id, option_id, expected_revision, session_token: ownerSession() }));
  },
  async image(outcome_id, image_proof_id) {
    const data = parseJSON(await callTool(DEFAULT_MCP_URL, "owner_dashboard_image",
      { outcome_id, image_proof_id, session_token: ownerSession() }));
    if (!IMAGE_MIMES.has(data.mime) || typeof data.base64 !== "string" ||
        data.base64.length > Math.ceil(MAX_IMAGE_BYTES * 4 / 3) + 8 ||
        !/^[A-Za-z0-9+/]*={0,2}$/.test(data.base64)) {
      throw Error("unverified image response");
    }
    const decoded = atob(data.base64);
    if (!decoded.length || decoded.length > MAX_IMAGE_BYTES) throw Error("image too large");
    const bytes = Uint8Array.from(decoded, c => c.charCodeAt(0));
    return URL.createObjectURL(new Blob([bytes], { type: data.mime }));
  },
};

let lastRevision = "";
let lastSnapshot = null;
let loading = false;
async function refresh() {
  if (loading) return;
  loading = true;
  refs.refresh.disabled = true;
  try {
    const response = await transport.overview();
    if (!["ready", "empty", "error"].includes(response.status)) throw Error("invalid status");
    if (response.reason === "owner_authorization_required") {
      sessionToken = "";
      lastSnapshot = null;
      lastRevision = "";
      renderOverview({ status: "empty", message: "Роль владельца не подтверждена", intents: [] });
      refs.connection.textContent = "Нет доступа";
      refs.notice.hidden = false;
      refs.notice.textContent = "Войдите с локальным паролем владельца.";
      return;
    }
    if (response.revision !== lastRevision || !lastSnapshot) {
      renderOverview(response);
      lastRevision = response.revision;
      lastSnapshot = response;
    }
    refs.connection.textContent = response.status === "error" ? "Ошибка данных" : "Подключено";
    if (["ready","empty"].includes(response.status)) {
      await markOwnerPanelOpened((report)=>reportExtension(DEFAULT_MCP_URL,
        {detail:"owner_notice_state:"+JSON.stringify(report)})).catch(()=>{});
    }
  } catch (error) {
    const needsGrant = error?.message === "owner login required";
    refs.connection.textContent = needsGrant ? "Нет роли" : "Нет связи";
    refs.notice.hidden = false;
    refs.notice.textContent = needsGrant
      ? "Введите пароль и нажмите «Войти»."
      : lastSnapshot
      ? "Сервер недоступен. Показан последний подтверждённый снимок."
      : "Сервер недоступен. Нет данных для отображения.";
    if (!lastSnapshot) {
      clear(refs.tree);
      refs.tree.append(node("p", "Нет соединения с сервером результатов", "empty"));
      renderSummary([]);
    }
  } finally {
    loading = false;
    refs.refresh.disabled = false;
  }
}

const proofUrls = new Map();
function releaseProofUrls() {
  for (const url of proofUrls.values()) URL.revokeObjectURL(url);
  proofUrls.clear();
}
let currentGallery = null;
let selected = new Set();
let galleryBusy = false;
let gallerySequence = 0;
function status(message) { refs.tasteStatus.textContent = message; }
function updateTasteButtons() {
  const buttons = refs.tasteActions.querySelectorAll("button");
  for (const button of buttons) {
    if (button.dataset.action === "compare") button.disabled = galleryBusy || selected.size !== 2;
    if (button.dataset.action === "choose") {
      button.disabled = galleryBusy || selected.size !== 1 ||
        currentGallery?.status !== "ready" ||
        !proofUrls.has([...selected][0]);
    }
  }
}
function renderGalleryOptions(gallery) {
  clear(refs.tasteOptions);
  clear(refs.tasteActions);
  selected = new Set();
  const options = Array.isArray(gallery.options) ? gallery.options : [];
  for (const option of options) {
    const card = node("article", null, "taste-option");
    card.append(node("h3", value(option.label)));
    const preview = node("div", null, "preview");
    const placeholder = node("span", "Проверка изображения…", "preview-message");
    preview.append(placeholder);
    card.append(preview);
    const toggle = node("button", "Добавить к сравнению");
    toggle.type = "button";
    toggle.addEventListener("click", () => {
      if (selected.has(option.id)) selected.delete(option.id);
      else if (selected.size < 2) selected.add(option.id);
      card.classList.toggle("selected", selected.has(option.id));
      toggle.textContent = selected.has(option.id) ? "Убрать из сравнения" : "Добавить к сравнению";
      updateTasteButtons();
    });
    card.append(toggle);
    refs.tasteOptions.append(card);
    transport.image(gallery.outcome_id, option.image_proof_id).then((url) => {
      if (currentGallery !== gallery || !refs.dialog.open) {
        URL.revokeObjectURL(url);
        return;
      }
      proofUrls.set(option.id, url);
      const image = node("img");
      image.alt = "Подтверждённый визуальный вариант: " + value(option.label);
      image.src = url; // Only our verified blob URL, never evidence.ref
      preview.replaceChildren(image);
      updateTasteButtons();
    }).catch(() => {
      if (currentGallery === gallery) preview.replaceChildren(
        node("span", "Превью недоступно. Выбор заблокирован.", "preview-message")
      );
    });
  }
  const compare = node("button", "Сравнить 2 варианта");
  compare.type = "button";
  compare.dataset.action = "compare";
  compare.addEventListener("click", () => {
    const ids = [...selected];
    for (const card of refs.tasteOptions.children) {
      const option = options[[...refs.tasteOptions.children].indexOf(card)];
      card.hidden = !ids.includes(option.id);
    }
    status("Сравнение двух вариантов. Закройте окно, чтобы вернуться ко всем вариантам.");
  });
  const choose = node("button", "Выбрать вариант", "primary");
  choose.type = "button";
  choose.dataset.action = "choose";
  choose.addEventListener("click", commitTasteChoice);
  refs.tasteActions.append(compare, choose);
  updateTasteButtons();
}
async function showGallery(outcomeId) {
  const sequence = ++gallerySequence;
  releaseProofUrls();
  currentGallery = null;
  clear(refs.tasteOptions);
  clear(refs.tasteActions);
  status("Загрузка подтверждённых изображений…");
  if (!refs.dialog.open) refs.dialog.showModal();
  try {
    const gallery = await transport.gallery(outcomeId);
    if (sequence !== gallerySequence || !refs.dialog.open) return;
    if (gallery.status !== "ready" && gallery.status !== "chosen") {
      status("Галерея пока недоступна: " + value(gallery.reason));
      return;
    }
    currentGallery = gallery;
    status(gallery.status === "chosen"
      ? "Выбор уже зафиксирован. Для изменения потребуется новая версия результата."
      : "Выберите один вариант вкуса. Это не оценка качества работы.");
    renderGalleryOptions(gallery);
  } catch (_) {
    if (sequence === gallerySequence)
      status("Не удалось загрузить галерею. Выбор не записан.");
  }
}
async function commitTasteChoice() {
  if (galleryBusy || selected.size !== 1 || currentGallery?.status !== "ready") return;
  const [optionId] = selected;
  if (!proofUrls.has(optionId)) return;
  galleryBusy = true;
  updateTasteButtons();
  status("Проверяем версию и фиксируем выбор…");
  try {
    const result = await transport.choose(currentGallery.outcome_id, optionId,
                                          currentGallery.revision);
    if (result.status !== "chosen" || !result.decision_id) {
      throw Error("no trusted decision receipt");
    }
    currentGallery.status = "chosen";
    currentGallery.revision = result.revision;
    status("Выбор записан как Decision " + value(result.decision_id) + ". Это не приёмка качества.");
    await refresh();
  } catch (_) {
    status("Выбор не записан: версия устарела, доступ запрещён или сервер недоступен.");
  } finally {
    galleryBusy = false;
    updateTasteButtons();
  }
}
refs.refresh.addEventListener("click", refresh);
refs.login.addEventListener("click", async () => {
  const password = refs.password.value;
  refs.password.value = "";
  refs.login.disabled = true;
  try {
    const result = await transport.login(password);
    if (result.status !== "ready" ||
        !/^dash-[A-Za-z0-9_-]{43}$/.test(result.session_token)) {
      throw Error("invalid owner login");
    }
    sessionToken = result.session_token;
    lastSnapshot = null;
    lastRevision = "";
    await refresh();
  } catch (_) {
    sessionToken = "";
    refs.connection.textContent = "Нет роли";
    refs.notice.hidden = false;
    refs.notice.textContent = "Вход не разрешён. Проверьте локальный пароль владельца.";
  } finally {
    refs.login.disabled = false;
  }
});
$("project-filter").addEventListener("change", () => {
  lastSnapshot = null;
  lastRevision = "";
  refresh();
});
$("close-dialog").addEventListener("click", () => refs.dialog.close());
refs.dialog.addEventListener("close", () => {
  gallerySequence++;
  currentGallery = null;
  releaseProofUrls();
  selected.clear();
});
window.addEventListener("pagehide", releaseProofUrls);
refresh();
setInterval(refresh, POLL_MS);
