// Роутер на hash: #/  #/d/{group}  #/c/{group}/{conversation}[/{m5}]
import { api } from "./api.js";
import { h } from "./dom.js";
import { pref, runtime, setPref } from "./state.js";
import { renderConversation } from "./components/conversation.js";
import { renderDatasets } from "./components/datasets.js";

const view = document.getElementById("view");

function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "c" && parts.length >= 3) return { name: "conversation", group: parts[1], conv: parts[2], focus: parts[3] };
  if (parts[0] === "d" && parts[1]) return { name: "datasets", group: parts[1] };
  return { name: "datasets" };
}

let lastKey = "";
function route() {
  const r = parseRoute();
  // смена якоря реплики внутри той же карточки не перерисовывает экран
  const key = r.name === "conversation" ? `c|${r.group}|${r.conv}` : `d|${r.group || ""}`;
  if (key === lastKey && r.name === "conversation") return;
  lastKey = key;
  renderNav(r);
  window.scrollTo(0, 0);
  if (r.name === "conversation") renderConversation(view, r);
  else renderDatasets(view, r);
}

function renderNav(r) {
  const nav = document.getElementById("nav");
  const group = r.group || pref("group") || runtime.config?.default_group_id || "DEV";
  nav.replaceChildren(
    h("a", { href: `#/d/${encodeURIComponent(group)}`, class: r.name === "datasets" ? "active" : "" }, "Наборы и диалоги"));
}

function setupTheme() {
  const btn = document.getElementById("theme-toggle");
  btn.addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme
      ? root.dataset.theme === "dark"
      : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    setPref("theme", root.dataset.theme);
  });
}

async function boot() {
  setupTheme();
  try {
    runtime.config = await api.config();
    const info = document.getElementById("model-info");
    info.textContent = runtime.config.llm_model;
    if (!runtime.config.llm_configured) {
      info.append(h("span", { class: "chip chip-bad", title: "ANTHROPIC_API_KEY не задан — оценка недоступна, просмотр работает" }, "нет ключа LLM"));
    }
  } catch { /* покажется на экранах */ }
  window.addEventListener("hashchange", route);
  route();
}

boot();
