// Экран «Наборы и диалоги»: загрузка набора по group_id, ручная загрузка JSON, список с фильтрами.
import { api } from "../api.js";
import { clear, errorBox, h, spinner, toast } from "../dom.js";
import { CHANNEL, EVAL_STATUS, GROSS } from "../labels.js";
import { pref, setPref, runtime } from "../state.js";
import { createBatchPanel, createStats } from "./batch.js";

const FILTERS = { channel: "", language: "", status: "", gross: "", appeal: "", q: "" };

export function renderDatasets(root, { group } = {}) {
  const groupId = group || pref("group") || runtime.config?.default_group_id || "DEV";
  clear(root);

  const input = h("input", { class: "input", id: "group-id", value: groupId, placeholder: "group_id, например DEV",
    autocomplete: "off", spellcheck: "false" });
  const loadBtn = h("button", { class: "btn btn-primary", type: "submit" }, "Загрузить набор");
  const notices = h("div", { class: "notices" });
  const fileInput = h("input", { type: "file", accept: ".json,application/json", class: "visually-hidden", id: "upload-file" });
  const uploadBtn = h("label", { class: "btn", for: "upload-file", title: "JSON: запись, массив записей или ответ API" }, "Загрузить JSON…");

  const form = h("form", { class: "toolbar", onsubmit: (e) => { e.preventDefault(); load(input.value.trim(), true); } },
    h("label", { class: "field" }, h("span", { class: "field-label" }, "Набор (group_id)"), input),
    loadBtn, uploadBtn, fileInput);

  const listBox = h("section", { class: "panel" });
  root.append(h("div", { class: "page" }, h("h1", { class: "page-title" }, "Наборы и диалоги"), form, notices, listBox));

  fileInput.addEventListener("change", async () => {
    const file = fileInput.files[0];
    if (!file) return;
    const target = input.value.trim() || "MANUAL";
    try {
      const r = await api.uploadConversations(file, target);
      toast(`Загружено диалогов: ${r.total} в набор ${r.group_id}`, "ok");
      showWarnings(r.warnings);
      go(r.group_id);
    } catch (e) {
      notices.replaceChildren(errorBox(e));
    } finally {
      fileInput.value = "";
    }
  });

  function showWarnings(ws) {
    notices.replaceChildren(...(ws || []).map((w) => h("div", { class: "alert alert-warn" }, w)));
  }

  function go(g) {
    setPref("group", g);
    if (location.hash !== `#/d/${encodeURIComponent(g)}`) location.hash = `#/d/${encodeURIComponent(g)}`;
    else showList(g);
  }

  async function load(g, refresh) {
    if (!g) return;
    loadBtn.disabled = true;
    notices.replaceChildren(spinner("Запрашиваю набор у API организаторов…"));
    try {
      const r = await api.loadDataset(g, refresh);
      const src = r.source === "cache" ? " (из локального кэша)" : "";
      toast(`Набор ${r.group_id}: ${r.total} диалогов${src}`, "ok");
      showWarnings(r.warnings);
      go(r.group_id);
    } catch (e) {
      notices.replaceChildren(errorBox(e));
    } finally {
      loadBtn.disabled = false;
    }
  }

  async function showList(g) {
    listBox.replaceChildren(spinner());
    let rows;
    try {
      rows = await api.listConversations(g);
    } catch (e) {
      listBox.replaceChildren(errorBox(e));
      return;
    }
    if (!rows.length) {
      listBox.replaceChildren(h("div", { class: "empty" },
        `Набор ${g} ещё не загружен. Нажмите «Загрузить набор».`));
      return;
    }
    renderList(listBox, g, rows, () => refreshRows(g));
  }

  // обновить строки без потери фильтров/выбора (во время пакетной оценки)
  async function refreshRows(g) {
    try {
      const rows = await api.listConversations(g);
      listBox.dispatchEvent(new CustomEvent("rows", { detail: rows }));
    } catch { /* следующий тик */ }
  }

  showList(groupId);
}

function evalStatus(row) {
  const e = row.evaluation;
  if (!e) return "none";
  if (e.pending_count > 0 || e.status === "partial") return "partial";
  return e.status;
}

function renderList(box, group, initialRows, refresh) {
  let rows = initialRows;
  const selected = new Set();
  const uniq = (key) => [...new Set(rows.map((r) => r[key]).filter(Boolean))].sort();
  const select = (key, label, options) => {
    const el = h("select", { class: "input input-sm", "aria-label": label,
      onchange: () => { FILTERS[key] = el.value; draw(); } },
    h("option", { value: "" }, label), options.map(([v, t]) => h("option", { value: v, selected: FILTERS[key] === v }, t)));
    return el;
  };
  const search = h("input", { class: "input input-sm", type: "search", placeholder: "Поиск по ID или тексту",
    value: FILTERS.q, oninput: () => { FILTERS.q = search.value.trim().toLowerCase(); draw(); } });
  const filters = h("div", { class: "filters" },
    select("channel", "Все каналы", uniq("channel").map((c) => [c, CHANNEL[c] || c])),
    select("language", "Все языки", uniq("language").map((l) => [l, l])),
    select("status", "Любой статус", [["none", "Не оценён"], ["done", "Оценён"], ["partial", "Требует проверки"]]),
    select("gross", "Грубые ошибки: все", [["yes", "Есть грубая ошибка"], ["no", "Без грубых ошибок"]]),
    select("appeal", "Апелляции: все", [["open", "Есть открытая апелляция"], ["any", "Есть апелляции"], ["none", "Без апелляций"]]),
    search);

  const stats = createStats(group);
  const batch = createBatchPanel({
    group,
    getSelected: () => [...selected],
    onProgress: () => { refresh(); stats.refresh(); },
    onFinished: () => { refresh(); stats.refresh(); },
  });

  const headCount = h("span", { class: "muted" });
  const counter = h("div", { class: "muted small" });
  const selectAll = h("input", { type: "checkbox", "aria-label": "Выбрать все показанные",
    onchange: () => { for (const r of visible()) selectAll.checked ? selected.add(r.conversation_id) : selected.delete(r.conversation_id); draw(); } });
  const tbody = h("tbody");
  const table = h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {},
      h("th", { class: "col-check" }, selectAll),
      h("th", {}, "Диалог"), h("th", { class: "col-opt" }, "Канал"), h("th", { class: "col-opt" }, "Язык"),
      h("th", { class: "num col-opt" }, "Реплик"), h("th", { class: "num" }, "Итог"), h("th", {}, "Статус"),
      h("th", { class: "col-opt" }, "Чек-лист"))),
    tbody));

  box.replaceChildren(
    h("div", { class: "panel-head" }, h("h2", {}, `Набор ${group}`), headCount),
    stats.el, batch.el, filters, counter, table);

  box.addEventListener("rows", (ev) => { rows = ev.detail; draw(); });

  function visible() {
    return rows.filter((r) => {
      if (FILTERS.channel && r.channel !== FILTERS.channel) return false;
      if (FILTERS.language && r.language !== FILTERS.language) return false;
      if (FILTERS.status && evalStatus(r) !== FILTERS.status) return false;
      if (FILTERS.gross === "yes" && !r.evaluation?.gross_error) return false;
      if (FILTERS.gross === "no" && r.evaluation?.gross_error) return false;
      if (FILTERS.appeal === "open" && !r.evaluation?.open_appeals) return false;
      if (FILTERS.appeal === "any" && !r.evaluation?.appeals_count) return false;
      if (FILTERS.appeal === "none" && r.evaluation?.appeals_count) return false;
      if (FILTERS.q && !(`${r.conversation_id} ${r.preview}`.toLowerCase().includes(FILTERS.q))) return false;
      return true;
    });
  }

  function draw() {
    const shown = visible();
    headCount.textContent = `${rows.length} диалогов, оценено ${rows.filter((r) => r.evaluation).length}`;
    counter.textContent = shown.length === rows.length ? "" : `Показано ${shown.length} из ${rows.length}`;
    selectAll.checked = shown.length > 0 && shown.every((r) => selected.has(r.conversation_id));
    tbody.replaceChildren(...shown.map((r) => row(group, r, selected, () => { draw(); })));
    batch.selectionChanged(selected.size);
  }
  draw();
}

function row(group, r, selected, onSelect) {
  const e = r.evaluation;
  const href = `#/c/${encodeURIComponent(group)}/${encodeURIComponent(r.conversation_id)}`;
  const st = evalStatus(r);
  const statusCell = h("td", {},
    st === "none" ? h("span", { class: "chip" }, "Не оценён")
      : h("span", { class: `chip chip-${st}` }, EVAL_STATUS[st] || st),
    e?.open_appeals ? h("span", { class: "chip chip-review chip-block", title: "Открытые апелляции" }, `апелляций: ${e.open_appeals}`) : null,
    e?.gross_error ? h("span", { class: "badge-gross", title: (e.gross_types || []).map((g) => GROSS[g] || g).join(", ") }, "Грубая ошибка") : null);
  const tr = h("tr", { class: "row-link", tabindex: "0", onclick: () => (location.hash = href),
    onkeydown: (ev) => { if (ev.key === "Enter") location.hash = href; } },
  h("td", { class: "col-check", onclick: (ev) => ev.stopPropagation() },
    h("input", { type: "checkbox", "aria-label": `Выбрать ${r.conversation_id}`, checked: selected.has(r.conversation_id),
      onchange: (ev) => { ev.target.checked ? selected.add(r.conversation_id) : selected.delete(r.conversation_id); onSelect(); } })),
  h("td", {}, h("a", { href, class: "conv-id" }, r.conversation_id),
    h("div", { class: "row-tags" }, `${CHANNEL[r.channel] || r.channel} · ${r.language}`),
    h("div", { class: "preview" }, r.preview)),
  h("td", { class: "col-opt" }, CHANNEL[r.channel] || r.channel, r.has_audio ? h("span", { class: "muted", title: "Есть аудиозапись" }, " ♪") : null),
  h("td", { class: "col-opt" }, r.language),
  h("td", { class: "num col-opt" }, r.messages_count),
  h("td", { class: "num total-cell" }, e ? (e.total ?? "—") : ""),
  statusCell,
  h("td", { class: "muted small col-opt" }, e ? `${e.checklist_id} v${e.checklist_version}` : ""));
  return tr;
}
