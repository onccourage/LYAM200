// Аналитика набора: все числа приходят с бэкенда (app/analytics.py), здесь только отрисовка.
// Графики — простые HTML-полосы одного цвета (без внешних библиотек), у каждого есть табличный вид.
import { api } from "../api.js";
import { clear, errorBox, h, spinner } from "../dom.js";
import { CHANNEL } from "../labels.js";
import { pref, setPref } from "../state.js";

const TYPE = { binary: "да/нет", scale: "шкала", flag: "флаг" };
let tip;

function tooltip() {
  if (!tip) {
    tip = h("div", { class: "chart-tip", role: "tooltip", hidden: true });
    document.body.append(tip);
  }
  return tip;
}

function hover(el, lines) {
  const show = (ev) => {
    const t = tooltip();
    t.replaceChildren(...lines.map((l, i) => h("div", { class: i ? "" : "tip-title" }, l)));
    t.hidden = false;
    const r = (ev.currentTarget || el).getBoundingClientRect();
    t.style.left = `${Math.min(window.innerWidth - t.offsetWidth - 8, Math.max(8, r.left + r.width / 2 - t.offsetWidth / 2))}px`;
    t.style.top = `${Math.max(8, r.top - t.offsetHeight - 8 + window.scrollY)}px`;
  };
  const hide = () => { if (tip) tip.hidden = true; };
  el.tabIndex = 0;
  el.addEventListener("mouseenter", show);
  el.addEventListener("focus", show);
  el.addEventListener("mouseleave", hide);
  el.addEventListener("blur", hide);
  return el;
}

const width = (v, max) => `${v === null || v === undefined ? 0 : Math.max(0, Math.min(100, (Number(v) / max) * 100))}%`;

/** Горизонтальные полосы, одна серия. items: {label, sub?, value, display, tip[]} */
function hbars(items, { max = 100, empty = "Нет данных" } = {}) {
  if (!items.length) return h("div", { class: "muted small" }, empty);
  return h("div", { class: "hbars", role: "list" }, items.map((it) =>
    h("div", { class: "hbar-row", role: "listitem" },
      h("div", { class: "hbar-label" }, it.label, it.sub ? h("div", { class: "muted small" }, it.sub) : null),
      h("div", { class: "hbar-track" },
        hover(h("div", { class: `hbar ${it.value === null ? "hbar-none" : ""}`, style: `width:${width(it.value, max)}`,
          "aria-label": `${it.label}: ${it.display}` }), it.tip)),
      h("div", { class: "hbar-val" }, it.display))));
}

function tile(label, value, hint) {
  return h("div", { class: "stat", title: hint || "" },
    h("div", { class: "stat-value" }, value ?? "—"), h("div", { class: "stat-label" }, label));
}

function card(title, note, chart, table) {
  const view = h("div", {}, chart);
  const btn = table ? h("button", { class: "btn btn-sm btn-ghost", type: "button", "aria-pressed": "false",
    onclick: () => {
      const showTable = btn.getAttribute("aria-pressed") !== "true";
      btn.setAttribute("aria-pressed", String(showTable));
      btn.textContent = showTable ? "График" : "Таблица";
      view.replaceChildren(showTable ? table : chart);
    } }, "Таблица") : null;
  return h("section", { class: "panel panel-stack" },
    h("div", { class: "panel-head" }, h("h2", {}, title), btn),
    note ? h("div", { class: "muted small" }, note) : null, view);
}

function table(head, rows) {
  return h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, head.map(([t, num]) => h("th", { class: num ? "num" : "" }, t)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, r.map((v, i) => h("td", { class: head[i][1] ? "num" : "" }, v ?? "—")))))));
}

export async function renderAnalytics(root, { group } = {}) {
  const g = group || pref("group") || "DEV";
  clear(root).append(spinner());
  let d;
  try {
    d = await api.analytics(g);
  } catch (e) {
    clear(root).append(errorBox(e));
    return;
  }
  setPref("group", g);
  const s = d.summary, ap = d.appeals;
  const groupInput = h("input", { class: "input input-sm", value: g, "aria-label": "group_id" });
  const head = h("div", { class: "editor-head" },
    h("h1", { class: "page-title" }, `Аналитика · ${g}`),
    h("form", { class: "toolbar", onsubmit: (e) => { e.preventDefault(); location.hash = `#/analytics/${encodeURIComponent(groupInput.value.trim())}`; } },
      groupInput, h("button", { class: "btn btn-sm", type: "submit" }, "Показать")));

  if (!d.evaluated) {
    clear(root).append(h("div", { class: "page" }, head,
      h("div", { class: "empty panel" }, `В наборе ${g} нет оценённых диалогов. Оцените набор на экране «Наборы и диалоги».`)));
    return;
  }

  const versions = d.checklist_versions.map((v) => `${v.checklist_id} v${v.version} (${v.dialogs})`).join(", ");
  const kpis = h("div", { class: "stats kpis" },
    tile("оценено диалогов", `${d.evaluated} / ${d.dialogs_total}`),
    tile("средний итог", s.avg_total, `медиана ${s.median_total ?? "—"}; не рассчитывается: ${s.not_calculated}`),
    tile("с грубой ошибкой", s.gross_dialogs ? `${s.gross_dialogs} · ${s.gross_rate}%` : "0"),
    tile("требуют проверки", s.needs_review_dialogs),
    tile("апелляций удовлетворено", ap.resolved ? `${ap.changed_rate}%` : "—",
      `Доля апелляций, где проверяющий изменил оценку модели, — оценка ошибок модели. Решено ${ap.resolved}, изменено ${ap.changed}, открыто ${ap.open}.`),
    tile("ср. стоимость диалога", d.cost.avg_cost_per_conversation ? `$${d.cost.avg_cost_per_conversation}` : "—",
      `Потрачено всего $${d.cost.spent}; токены на оценку ${d.cost.avg_tokens_in ?? "—"} / ${d.cost.avg_tokens_out ?? "—"}`));

  // --- критерии: доля «Нет» / не максимум / нарушение, по убыванию
  const ranked = [...d.criteria].sort((a, b) => (Number(b.problem_rate) || -1) - (Number(a.problem_rate) || -1));
  const critChart = hbars(ranked.map((c) => ({
    label: `${c.code} ${c.title}`,
    sub: `${TYPE[c.type]} · доля: ${c.problem_label}`,
    value: c.problem_rate,
    display: c.problem_rate === null ? "—" : `${c.problem_rate}% (${c.problem} из ${c.applicable})`,
    tip: [`${c.code} ${c.title}`, `${c.problem_label}: ${c.problem} из ${c.applicable} применимых`,
      `N/A: ${c.na}${c.na_rate !== null ? ` (${c.na_rate}%)` : ""}`, `требуют проверки: ${c.needs_review}`,
      c.avg_points_pct !== null ? `средний балл: ${c.avg_points_pct}% от веса` : "без баллов (флаг)",
      c.consensus_answers ? `проходы модели разошлись: ${c.split} из ${c.consensus_answers}` : ""].filter(Boolean),
  })));
  const critTable = table(
    [["Критерий"], ["Тип"], ["Да", 1], ["Нет", 1], ["Ступени"], ["N/A", 1], ["Проверка", 1], ["Низкая увер.", 1], ["Расхождения проходов", 1], ["Ср. балл, % веса", 1], ["Проблемных, %", 1]],
    d.criteria.map((c) => [`${c.code} ${c.title}`, TYPE[c.type], c.type === "scale" ? "" : c.yes, c.type === "scale" ? "" : c.no,
      Object.entries(c.steps).map(([k, n]) => `${k}: ${n}`).join(", "), c.na, c.needs_review, c.low_confidence,
      c.consensus_answers ? `${c.split} из ${c.consensus_answers}` : "—", c.avg_points_pct, c.problem_rate]));

  // --- распределение итогов: колонки
  const maxCount = Math.max(1, ...d.histogram.map((b) => b.count));
  const hist = h("div", { class: "columns", role: "list" }, d.histogram.map((b) =>
    h("div", { class: "col", role: "listitem" },
      h("div", { class: "col-val" }, b.count ? String(b.count) : ""),
      h("div", { class: "col-track" }, hover(h("div", { class: "col-bar", style: `height:${(b.count / maxCount) * 100}%`,
        "aria-label": `${b.label}: ${b.count}` }), [`Итог ${b.label}`, `${b.count} диалогов`, b.share !== null ? `${b.share}% оценённых` : ""])),
      h("div", { class: "col-label" }, b.label))));
  const histTable = table([["Итог"], ["Диалогов", 1], ["Доля, %", 1]], d.histogram.map((b) => [b.label, b.count, b.share]));

  // --- языки и каналы
  const sliceTable = (rows, labelFn) => h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", { class: "num" }, "Диалогов"), h("th", {}, "Средний итог"),
      h("th", { class: "num" }, "Мин–макс"), h("th", { class: "num" }, "Грубые"), h("th", { class: "num" }, "Проверка"))),
    h("tbody", {}, rows.map((r) => h("tr", {},
      h("td", {}, h("strong", {}, labelFn(r.key))), h("td", { class: "num" }, r.dialogs),
      h("td", {}, h("div", { class: "meter-row" }, h("div", { class: "meter" }, hover(h("div", { class: "meter-fill", style: `width:${width(r.avg_total, 100)}` }),
        [labelFn(r.key), `средний итог ${r.avg_total ?? "—"}`, `${r.dialogs} диалогов`])), h("span", { class: "meter-val" }, r.avg_total ?? "—"))),
      h("td", { class: "num muted" }, r.min_total ? `${r.min_total}–${r.max_total}` : "—"),
      h("td", { class: "num" }, r.gross ? `${r.gross} (${r.gross_rate}%)` : "0"),
      h("td", { class: "num" }, r.needs_review))))));

  // --- грубые ошибки
  const maxType = Math.max(1, ...d.gross.by_type.map((t) => t.count));
  const grossChart = hbars(d.gross.by_type.map((t) => ({ label: t.label, value: (t.count / maxType) * 100, display: String(t.count),
    tip: [t.label, `${t.count} диалогов`] })), { empty: "Грубых ошибок не найдено" });

  // --- апелляции
  const appealsBody = ap.total ? h("div", { class: "panel-stack" },
    h("div", { class: "stats" },
      tile("подано", ap.total), tile("решено", ap.resolved), tile("изменено", ap.changed), tile("подтверждено", ap.confirmed),
      tile("ср. сдвиг итога", ap.avg_total_shift !== null ? (Number(ap.avg_total_shift) > 0 ? `+${ap.avg_total_shift}` : ap.avg_total_shift) : "—",
        "Средний сдвиг итога диалога после изменения оценки проверяющим")),
    ap.by_criterion.length ? hbars(ap.by_criterion.map((c) => ({ label: `${c.criterion_id} ${c.title}`, value: c.changed_rate,
      display: `${c.changed_rate}% (${c.changed} из ${c.resolved})`, tip: [`${c.criterion_id} ${c.title}`, `изменено ${c.changed} из ${c.resolved} решённых`] }))) : null)
    : h("div", { class: "muted small" }, "Апелляций пока нет.");

  clear(root).append(h("div", { class: "page" }, head,
    h("div", { class: "muted small" }, `Текущие оценки (с учётом решений по апелляциям). Версии чек-листа: ${versions}.`),
    kpis,
    card("Где операторы теряют баллы", "Доля «Нет» для да/нет, доля оценок ниже максимума для шкал, доля нарушений для флагов — среди применимых (без N/A).",
      critChart, critTable),
    h("div", { class: "analytics-grid" },
      card("Распределение итогов", `Не рассчитывается (нет применимых критериев): ${s.not_calculated}.`, hist, histTable),
      card("Грубые ошибки по типам", `Диалогов с грубой ошибкой: ${d.gross.dialogs}${d.gross.rate ? ` (${d.gross.rate}%)` : ""}. Итог не обнуляют.`, grossChart, null)),
    h("div", { class: "analytics-grid" },
      card("Качество по языкам", null, sliceTable(d.by_language, (k) => k), null),
      card("Качество по каналам", null, sliceTable(d.by_channel, (k) => CHANNEL[k] || k), null)),
    card("Апелляции — оценка ошибок модели", "Удовлетворённая апелляция = проверяющий изменил оценку модели.", appealsBody, null)));
}
