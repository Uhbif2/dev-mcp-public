// Installation-specific addresses belong to local-settings.json (never distributed).
export const INSTALL_DEFAULTS = {
  enabled: false,
  projects: [],
  projectUrl: "https://chatgpt.com/",
  connector: "dev-mcp",
};

let local;
export function localSettings() {
  return local ||= (async () => {
    try {
      const reply = await fetch(chrome.runtime.getURL("local-settings.json"));
      if (!reply.ok) return {};
      const value = await reply.json();
      if (!value || Array.isArray(value) || typeof value !== "object") return {};
      return Object.fromEntries(Object.keys(INSTALL_DEFAULTS)
        .filter((key) => key in value && (key === "projects"
          ? Array.isArray(value[key]) && value[key].every((name) => typeof name === "string")
          : typeof value[key] === typeof INSTALL_DEFAULTS[key]))
        .map((key) => [key, value[key]]));
    } catch { return {}; }
  })();
}
