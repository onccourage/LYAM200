// Мини-хелперы DOM. Весь пользовательский текст — только через textContent (без innerHTML).
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === undefined || c === null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) {
  while (el.firstChild) el.firstChild.remove();
  return el;
}

export function fmtSec(sec) {
  if (sec === null || sec === undefined) return "";
  const s = Math.max(0, Math.round(sec));
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  const p = (n) => String(n).padStart(2, "0");
  return hh ? `${hh}:${p(mm)}:${p(ss)}` : `${p(mm)}:${p(ss)}`;
}

export function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "short" });
}

let toastTimer;
export function toast(message, kind = "info") {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = message;
  el.className = `toast show toast-${kind}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = "toast"), kind === "error" ? 7000 : 3500);
}

export function spinner(text = "Загрузка…") {
  return h("div", { class: "loading" }, h("span", { class: "spin" }), text);
}

export function errorBox(err) {
  return h("div", { class: "alert alert-error" }, err && err.message ? err.message : String(err));
}
