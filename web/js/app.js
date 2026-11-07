// Роутер на hash: #/  #/d/{group}  #/c/{group}/{conversation}[/{m5}]
import { api } from "./api.js";
import { h } from "./dom.js";
import { ROLES, pref, role, runtime, setPref } from "./state.js";
import { renderAppeal, renderAppealsList } from "./components/appeals.js";
import { renderChecklistEditor } from "./components/checklist.js";
import { renderConversation } from "./components/conversation.js";
import { renderDatasets } from "./components/datasets.js";

const view = document.getElementById("view");

function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "c" && parts.length >= 3) return { name: "conversation", group: parts[1], conv: parts[2], focus: parts[3] };
  if (parts[0] === "d" && parts[1]) return { name: "datasets", group: parts[1] };
  if (parts[0] === "appeals") return { name: "appeals", status: parts[1] || "open" };
  if (parts[0] === "a" && parts[1]) return { name: "appeal", id: Number(parts[1]) };
  if (parts[0] === "checklist") return { name: "checklist", id: parts[1], version: parts[2] ? Number(parts[2]) : undefined };
  return { name: "datasets" };
}

let lastKey = "";
function route(force = false) {
  const r = parseRoute();
  // смена якоря реплики внутри той же карточки не перерисовывает экран
  const key = r.name === "conversation" ? `c|${r.group}|${r.conv}` : `${r.name}|${r.group || r.id || r.status || ""}|${r.version || ""}`;
  if (!force && key === lastKey && r.name === "conversation") return;
  lastKey = key;
  renderNav(r);
  window.scrollTo(0, 0);
  if (r.name === "conversation") renderConversation(view, r);
  else if (r.name === "appeals") renderAppealsList(view, r);
  else if (r.name === "appeal") renderAppeal(view, r);
  else if (r.name === "checklist") renderChecklistEditor(view, r);
  else renderDatasets(view, r);
}

function renderNav(r) {
  const nav = document.getElementById("nav");
  const group = r.group || pref("group") || runtime.config?.default_group_id || "DEV";
  const appealsLink = h("a", { href: "#/appeals/open", class: r.name === "appeals" || r.name === "appeal" ? "active" : "" },
    "Апелляции", h("span", { class: "nav-count", id: "appeals-count", hidden: true }));
  nav.replaceChildren(
    h("a", { href: `#/d/${encodeURIComponent(group)}`, class: r.name === "datasets" || r.name === "conversation" ? "active" : "" }, "Наборы и диалоги"),
    appealsLink,
    h("a", { href: "#/checklist", class: r.name === "checklist" ? "active" : "" }, "Чек-лист"));
  refreshAppealsCount();
}

async function refreshAppealsCount() {
  try {
    const { open } = await api.appealsSummary();
    const el = document.getElementById("appeals-count");
    if (el) { el.textContent = String(open); el.hidden = !open; }
  } catch { /* не критично */ }
}

function setupRole() {
  const box = document.getElementById("role-switch");
  const draw = () => box.replaceChildren(...Object.entries(ROLES).map(([key, label]) =>
    h("button", { type: "button", class: role() === key ? "active" : "", "aria-pressed": String(role() === key),
      onclick: () => { setPref("role", key); draw(); route(true); } }, label)));
  draw();
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
  setupRole();
  document.addEventListener("appeals-changed", refreshAppealsCount);
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
