// Апелляции: список (открытые / решённые) и экран проверяющего с решением.
import { api } from "../api.js";
import { clear, errorBox, fmtDate, h, spinner, toast } from "../dom.js";
import { CHANNEL, DECISION, GROSS, answerLabel, answerOptions } from "../labels.js";
import { pref, role, setPref } from "../state.js";
import { createTranscript } from "./transcript.js";

const ans = (a, type) => (a === null || a === undefined ? "Требует проверки" : answerLabel(a, type));

export async function renderAppealsList(root, { status = "open" } = {}) {
  clear(root).append(spinner());
  let rows;
  try {
    rows = await api.listAppeals(status === "all" ? null : status);
  } catch (e) {
    clear(root).append(errorBox(e));
    return;
  }
  const tab = (key, label) => h("a", { class: `tab ${status === key ? "active" : ""}`, href: `#/appeals/${key}` }, label);
  const body = rows.length ? h("div", { class: "table-wrap" }, h("table", { class: "table" },
    h("thead", {}, h("tr", {}, h("th", {}, "№"), h("th", {}, "Диалог"), h("th", {}, "Критерий"),
      h("th", {}, "Комментарий"), h("th", {}, "Решение"), h("th", { class: "num" }, "Итог"), h("th", { class: "col-opt" }, "Подана"))),
    h("tbody", {}, rows.map((a) => {
      const href = `#/a/${a.id}`;
      return h("tr", { class: "row-link", tabindex: "0", onclick: () => (location.hash = href),
        onkeydown: (e) => { if (e.key === "Enter") location.hash = href; } },
      h("td", {}, h("a", { href }, `#${a.id}`)),
      h("td", {}, h("span", { class: "conv-id" }, a.conversation_id), h("div", { class: "muted small" }, a.group_id)),
      h("td", {}, a.criterion_id),
      h("td", {}, h("div", { class: "preview" }, a.comment)),
      h("td", {}, a.status === "open" ? h("span", { class: "chip chip-partial" }, "На рассмотрении")
        : [h("span", { class: `chip ${a.decision === "changed" ? "chip-mid" : "chip-na"}` }, DECISION[a.decision]),
          h("div", { class: "muted small" }, `${ans(a.original_answer)} → ${ans(a.new_answer)}`)]),
      h("td", { class: "num" }, a.status === "open" ? (a.original_total ?? "—") : `${a.original_total ?? "—"} → ${a.final_total ?? "—"}`),
      h("td", { class: "muted small col-opt" }, fmtDate(a.created_at), a.submitter_label ? h("div", {}, a.submitter_label) : null));
    })))) : h("div", { class: "empty" }, status === "open" ? "Открытых апелляций нет." : "Апелляций нет.");

  clear(root).append(h("div", { class: "page" },
    h("h1", { class: "page-title" }, "Апелляции"),
    role() === "controller" ? h("div", { class: "alert alert-warn small" },
      "Вы в роли «Контролёр»: апелляции подаются из карточки диалога. Чтобы рассматривать их, переключитесь на «Проверяющий» в шапке.") : null,
    h("section", { class: "panel" },
      h("div", { class: "tabs" }, tab("open", "Открытые"), tab("resolved", "Решённые"), tab("all", "Все")),
      body)));
}

export async function renderAppeal(root, { id }) {
  clear(root).append(spinner());
  let d;
  try {
    d = await api.getAppeal(id);
  } catch (e) {
    clear(root).append(errorBox(e));
    return;
  }
  const { appeal: a, criterion: c, verdict: v, score_row: row, evaluation: ev, conversation: conv, history } = d;
  const transcript = createTranscript(conv);
  const convHref = `#/c/${encodeURIComponent(ev.group_id)}/${encodeURIComponent(ev.conversation_id)}`;

  // --- спорная оценка (как её поставила модель)
  const evidence = (v?.evidence || []).map((e) => h("button", { class: "evidence", type: "button",
    onclick: () => transcript.focus([e.message_id], { play: true }) },
  h("span", { class: "ev-ref" }, `${e.message_id} · ${e.timestamp || ""}`), h("span", { class: "ev-quote" }, `«${e.quote}»`)));
  const original = a.status === "open" ? (v?.status === "ok" ? v.answer : null) : a.original_answer;

  const disputed = h("section", { class: "panel panel-stack" },
    h("div", { class: "panel-head" }, h("h2", {}, `Апелляция #${a.id}`),
      a.status === "open" ? h("span", { class: "chip chip-partial" }, "На рассмотрении")
        : h("span", { class: `chip ${a.decision === "changed" ? "chip-mid" : "chip-na"}` }, DECISION[a.decision])),
    h("div", { class: "crit-head" },
      h("div", {}, h("span", { class: "crit-code" }, c.code || c.id), " ", h("span", { class: "crit-title" }, c.title)),
      h("span", { class: "chip" }, { binary: "да/нет", scale: "шкала", flag: "флаг" }[c.type])),
    c.description ? h("p", { class: "muted small" }, c.description) : null,
    c.type === "scale" ? h("ul", { class: "scale-list small" }, [...c.scale].sort((x, y) => y.value - x.value)
      .map((s) => h("li", { class: String(s.value) === String(original) ? "current" : "" }, h("strong", {}, s.value), ` — ${s.label}`))) : null,
    c.na_allowed ? h("div", { class: "muted small" }, `N/A разрешён: ${c.na_condition}`) : h("div", { class: "muted small" }, "N/A не разрешён"),
    h("h3", { class: "crit-group-title" }, "Оценка модели"),
    h("div", { class: "crit" },
      h("div", { class: "crit-head" }, h("strong", {}, ans(original, c.type)),
        a.original_points ? h("span", { class: "muted" }, `${a.original_points} из ${c.weight} б.`) : null),
      v?.rationale ? h("p", { class: "rationale" }, v.rationale) : null,
      evidence.length ? h("div", { class: "evidence-list" }, evidence) : null,
      v?.checked_range ? h("button", { class: "evidence evidence-range", type: "button",
        onclick: () => transcript.focusRange(v.checked_range.from, v.checked_range.to) },
      h("span", { class: "ev-ref" }, `Проверено ${v.checked_range.from}–${v.checked_range.to}`)) : null,
      v?.na_reason ? h("div", { class: "na-reason" }, `N/A: ${v.na_reason}`) : null,
      v?.review_reason ? h("div", { class: "alert alert-warn small" }, v.review_reason) : null),
    h("h3", { class: "crit-group-title" }, "Комментарий контролёра"),
    h("blockquote", { class: "comment" }, a.comment,
      h("div", { class: "muted small" }, `${a.submitter_label || "без подписи"} · ${fmtDate(a.created_at)}`)),
    h("div", { class: "muted small" },
      "Диалог: ", h("a", { href: convHref }, ev.conversation_id), ` · ${CHANNEL[conv.channel] || conv.channel} · ${conv.language} · `,
      `чек-лист ${ev.checklist_id} v${ev.checklist_version} · итог сейчас ${ev.total ?? "—"}`));

  const decisionBox = a.status === "open" ? decisionForm(a, c, original) : resultBlock(a, c, ev);
  const historyBox = history.length > 1 ? h("section", { class: "panel" },
    h("h3", { class: "crit-group-title" }, "Все апелляции по этой оценке"),
    h("ol", { class: "history-list" }, history.map((x) => h("li", {},
      h("a", { href: `#/a/${x.id}` }, `#${x.id}`), ` ${x.criterion_id}: `,
      x.status === "open" ? "на рассмотрении" : `${DECISION[x.decision]} (${ans(x.original_answer)} → ${ans(x.new_answer)}), итог ${x.original_total ?? "—"} → ${x.final_total ?? "—"}`)))) : null;

  clear(root).append(h("div", { class: "page page-wide" },
    h("a", { class: "back", href: "#/appeals/open" }, "← К апелляциям"),
    h("div", { class: "conv-grid" }, transcript.el,
      h("div", { class: "right-col stack" }, disputed, decisionBox, historyBox))));

  const firstEv = (v?.evidence || []).map((e) => e.message_id);
  requestAnimationFrame(() => {
    if (firstEv.length) transcript.focus(firstEv);
    else if (v?.checked_range) transcript.focusRange(v.checked_range.from, v.checked_range.to);
  });
}

function decisionForm(a, c, original) {
  const panel = h("section", { class: "panel" }, h("div", { class: "panel-head" }, h("h2", {}, "Решение проверяющего")));
  if (role() !== "reviewer") {
    panel.append(h("div", { class: "alert alert-warn small" },
      "Рассматривать апелляции может только «Проверяющий». Переключите роль в шапке."));
    return panel;
  }
  const radio = (value, label, checked) => h("label", { class: "check" },
    h("input", { type: "radio", name: "decision", value, checked, onchange: sync }), " ", label);
  const confirmR = radio("confirmed", "Подтвердить оценку", original !== null);
  const changeR = radio("changed", "Изменить оценку", original === null);
  if (original === null) confirmR.querySelector("input").disabled = true;

  const opts = answerOptions(c).filter(([val]) => val !== original);
  const select = h("select", { class: "input", "aria-label": "Новое значение", onchange: sync },
    opts.map(([val, label]) => h("option", { value: val }, label)));
  const grossBox = h("div", { class: "gross-pick", hidden: true },
    h("div", { class: "muted small" }, "Тип нарушения (хотя бы один):"),
    Object.entries(GROSS).map(([k, label]) => h("label", { class: "check" }, h("input", { type: "checkbox", value: k }), " ", label)));
  const newRow = h("label", { class: "field" }, h("span", { class: "field-label" }, "Новое значение"), select, grossBox);
  const reason = h("textarea", { class: "input textarea", rows: "3", placeholder: "Причина решения (обязательно)" });
  const name = h("input", { class: "input", placeholder: "Имя проверяющего", value: pref("reviewer", "") });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "Сохранить решение");

  function decision() { return panel.querySelector('input[name="decision"]:checked')?.value; }
  function sync() {
    newRow.hidden = decision() !== "changed";
    grossBox.hidden = !(c.type === "flag" && select.value === "yes");
  }

  const form = h("form", { class: "decision-form", onsubmit: async (e) => {
    e.preventDefault();
    if (!reason.value.trim()) { reason.focus(); toast("Укажите причину решения", "error"); return; }
    const payload = { decision: decision(), reason: reason.value.trim(), reviewer_label: name.value.trim() || null };
    if (payload.decision === "changed") {
      payload.new_answer = select.value;
      if (!grossBox.hidden) payload.new_gross_types = [...grossBox.querySelectorAll("input:checked")].map((x) => x.value);
    }
    submit.disabled = true;
    try {
      setPref("reviewer", name.value.trim() || null);
      const d = await api.resolveAppeal(a.id, payload);
      toast(`Решение сохранено. Итог: ${d.appeal.original_total ?? "—"} → ${d.appeal.final_total ?? "—"}`, "ok");
      renderAppeal(document.getElementById("view"), { id: a.id });
      document.dispatchEvent(new CustomEvent("appeals-changed"));
    } catch (err) {
      toast(err.message, "error");
      submit.disabled = false;
    }
  } },
  h("div", { class: "radios" }, confirmR, changeR), newRow,
  h("label", { class: "field" }, h("span", { class: "field-label" }, "Причина"), reason),
  h("label", { class: "field" }, h("span", { class: "field-label" }, "Проверяющий"), name),
  h("div", { class: "actions" }, submit));
  panel.append(form);
  sync();
  return panel;
}

function resultBlock(a, c, ev) {
  return h("section", { class: "panel" },
    h("div", { class: "panel-head" }, h("h2", {}, "Результат")),
    h("dl", { class: "meta meta-lg" },
      h("dt", {}, "Исходная оценка"), h("dd", {}, `${ans(a.original_answer, c.type)}${a.original_points ? ` · ${a.original_points} б.` : ""}`),
      h("dt", {}, "Решение"), h("dd", {}, `${DECISION[a.decision]}${a.new_gross_types?.length ? ` (${a.new_gross_types.map((g) => GROSS[g] || g).join(", ")})` : ""}`),
      h("dt", {}, "Финальная оценка"), h("dd", {}, h("strong", {}, ans(a.new_answer, c.type)), a.new_points ? ` · ${a.new_points} б.` : ""),
      h("dt", {}, "Итог диалога"), h("dd", {}, h("strong", {}, `${a.original_total ?? "—"} → ${a.final_total ?? "—"}`)),
      h("dt", {}, "Причина"), h("dd", {}, a.reason),
      h("dt", {}, "Кто и когда"), h("dd", {}, `${a.reviewer_label || "без подписи"} · ${fmtDate(a.resolved_at)}`),
      ev.original_total && ev.original_total !== ev.total ? [h("dt", {}, "Итог модели"), h("dd", {}, `${ev.original_total} (до всех апелляций)`)] : null));
}
