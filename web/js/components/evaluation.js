// Панель результата: итог, секции, грубая ошибка, метаданные, критерии с доказательствами.
// Все числа — строки, посчитанные бэкендом (scoring.py). Фронтенд ничего не округляет и не считает.
import { api } from "../api.js";
import { fmtDate, fmtSec, h, toast } from "../dom.js";
import { DECISION, GROSS, LOW_CONFIDENCE, answerLabel } from "../labels.js";
import { pref, role, setPref } from "../state.js";

export function createEvaluationPanel({ evaluation, checklist, active, conversation, transcript, onReevaluate, onChanged }) {
  const appeals = evaluation.appeals || [];
  const flagLabels = Object.fromEntries(checklist.criteria.flatMap((c) => (c.flag_types || []).map((f) => [f.id, f.label || f.id])));
  const ctx = { evaluation, appeals, onChanged, role: role(), flagLabels };
  const score = evaluation.score || {};
  const rows = new Map((score.criteria || []).map((c) => [c.criterion_id, c]));
  const verdicts = new Map((evaluation.answers || []).map((v) => [v.criterion_id, v]));
  const messages = new Map(conversation.messages.map((m) => [m.id, m]));
  const outdated = active && (active.id !== evaluation.checklist_id || active.version !== evaluation.checklist_version);

  // --- итог
  const total = evaluation.total;
  const pending = score.pending || [];
  const summary = h("div", { class: "summary" },
    h("div", { class: "total" },
      h("div", { class: "total-value" }, total ?? "—"),
      h("div", { class: "total-label" }, total === null || total === undefined ? "не рассчитывается: нет применимых критериев" : "из 100"),
      evaluation.original_total !== null && evaluation.original_total !== undefined && evaluation.original_total !== total
        ? h("div", { class: "orig-total", title: "Итог модели до решений по апелляциям" }, `модель: ${evaluation.original_total} → после апелляций: ${total ?? "—"}`) : null),
    h("div", { class: "summary-side" },
      score.gross_error ? grossBadge(score, verdicts, transcript, flagLabels) : null,
      pending.length ? h("div", { class: "alert alert-warn small" },
        `Требуют ручной проверки: ${pending.join(", ")}. Итог посчитан без них.`) : null,
      h("dl", { class: "meta" },
        metaRow("Чек-лист", `${checklist.name} · v${evaluation.checklist_version}`,
          outdated ? h("span", { class: "chip chip-partial" },
            active.id === evaluation.checklist_id ? `текущая v${active.version}` : `выбран «${active.name}» v${active.version}`) : null),
        metaRow("Модель", `${evaluation.model} · промпт ${evaluation.prompt_version}${evaluation.samples > 1 ? ` · консенсус ×${evaluation.samples}` : ""}`),
        metaRow("Стоимость", `$${evaluation.cost} · вызовов LLM: ${evaluation.llm_calls} · токены ${evaluation.tokens_in}/${evaluation.tokens_out}`),
        metaRow("Оценено", fmtDate(evaluation.created_at),
          evaluation.origin === "seed" ? h("span", { class: "chip chip-partial", title: "Оценка не делалась на этой машине — загружена из data/seed (предрасчитано)" }, "из сида") : null)),
      h("div", { class: "actions" },
        h("button", { class: `btn ${outdated ? "btn-primary" : ""}`, type: "button", onclick: () => onReevaluate({ full: false }),
          title: "Оценить последней версией выбранного чек-листа; неизменённые критерии берутся из кэша" },
        active ? `Переоценить: v${active.version}${active.id !== evaluation.checklist_id ? ` «${active.name}»` : ""}` : "Переоценить текущей версией"),
        h("button", { class: "btn btn-ghost", type: "button", onclick: () => onReevaluate({ full: true }),
          title: "Сбросить кэш критериев для диалога и оценить заново" }, "Переоценить полностью"))));

  // --- секции
  const sections = h("div", { class: "sections" }, (score.sections || []).filter((s) => s.has_weights).map((s) =>
    h("div", { class: "section-row" },
      h("div", { class: "section-name" }, s.title || s.section_id),
      h("div", { class: "bar", role: "img", "aria-label": `${s.pct ?? "—"}%` },
        h("div", { class: "bar-fill", style: `width:${s.pct === null ? 0 : Math.max(0, Math.min(100, Number(s.pct)))}%` })),
      h("div", { class: "section-val" }, s.pct === null ? "N/A" : `${s.pct}%`),
      h("div", { class: "muted small section-pts" }, s.pct === null ? "нет применимых" : `${s.points} из ${s.applicable_weight}`))));

  // --- критерии по секциям
  const sectionTitles = new Map(checklist.sections.map((s) => [s.id, s.title]));
  const order = [...checklist.sections].sort((a, b) => a.order - b.order).map((s) => s.id);
  const crits = checklist.criteria.filter((c) => c.enabled !== false);
  const groups = order.map((sid) => [sid, crits.filter((c) => c.section === sid)]).filter(([, cs]) => cs.length);
  const criteriaEl = h("div", { class: "criteria" }, groups.map(([sid, cs]) =>
    h("div", { class: "crit-group" }, h("h3", { class: "crit-group-title" }, sectionTitles.get(sid) || sid),
      cs.map((c) => criterionCard(c, verdicts.get(c.id), rows.get(c.id), messages, transcript, ctx)))));

  const titles = new Map(checklist.criteria.map((c) => [c.id, `${c.code || c.id} ${c.title}`]));
  const history = appeals.length ? h("div", { class: "history" },
    h("h3", { class: "crit-group-title" }, "История апелляций"),
    h("ol", { class: "history-list" }, appeals.map((a) => h("li", {},
      h("a", { href: `#/a/${a.id}` }, `#${a.id}`), " ",
      h("strong", {}, titles.get(a.criterion_id) || a.criterion_id), " · ",
      a.status === "open" ? h("span", { class: "chip chip-partial" }, "на рассмотрении")
        : [h("span", { class: `chip ${a.decision === "changed" ? "chip-mid" : "chip-na"}` }, DECISION[a.decision]),
          ` было ${answerText(a.original_answer)} → стало ${answerText(a.new_answer)}; итог ${a.original_total ?? "—"} → ${a.final_total ?? "—"}`],
      h("div", { class: "muted small" },
        `Подал${a.submitter_label ? `: ${a.submitter_label}` : "о"} ${fmtDate(a.created_at)} — «${a.comment}»`,
        a.status === "resolved" ? ` · Решил${a.reviewer_label ? `: ${a.reviewer_label}` : "о"} ${fmtDate(a.resolved_at)} — «${a.reason}»` : ""))))) : null;

  return h("section", { class: "panel eval-panel" },
    h("div", { class: "panel-head" }, h("h2", {}, "Оценка"),
      !evaluation.is_current ? h("span", { class: "chip chip-bad" }, "устаревшая оценка") : null),
    summary, sections, criteriaEl, history, provenance(evaluation, conversation));
}

// Происхождение оценки: что именно отправлено модели и что она ответила (прозрачность процесса).
function provenance(evaluation, conversation) {
  const out = h("div", { class: "prov-body" });
  const load = (fn) => async () => {
    out.replaceChildren(h("div", { class: "muted small" }, "Загрузка…"));
    try { out.replaceChildren(...await fn()); } catch (e) { out.replaceChildren(h("div", { class: "alert alert-error small" }, e.message)); }
  };
  const g = conversation.group_id, c = conversation.conversation_id;
  const pre = (obj) => h("pre", { class: "raw" }, typeof obj === "string" ? obj : JSON.stringify(obj, null, 2));
  return h("details", { class: "provenance" },
    h("summary", {}, "Происхождение оценки: промпт и сырой ответ модели"),
    h("div", { class: "muted small" },
      `${evaluation.origin === "seed" ? "Загружено из сида (оценено заранее). " : "Оценено на этом сервере. "}` +
      `Модель ${evaluation.model}, рамка промпта ${evaluation.prompt_version}, вызовов LLM ${evaluation.llm_calls}, токены ${evaluation.tokens_in}/${evaluation.tokens_out}. ` +
      "Критерии из кэша не вызывали модель повторно — их исходный ответ в той оценке, где они считались."),
    h("div", { class: "actions" },
      h("button", { class: "btn btn-sm", type: "button", onclick: load(async () => {
        const ev = await api.getEvaluation(g, c, true);
        if (!ev.raw_responses?.length) return [h("div", { class: "muted small" }, "Сырых ответов нет: все критерии взяты из кэша или правилами.")];
        return ev.raw_responses.map((r, i) => h("div", {},
          h("div", { class: "small" }, h("strong", {}, `Вызов ${i + 1}`), ` · критерии: ${(r.criteria || []).join(", ")}${r.retry ? " · повтор после ошибок проверки" : ""}`,
            r.usage ? ` · токены ${r.usage.input_tokens}/${r.usage.output_tokens}` : "", r.error ? ` · ошибка: ${r.error}` : ""),
          pre(r.data ?? r.error ?? r)));
      }) }, "Сырой ответ модели"),
      h("button", { class: "btn btn-sm", type: "button", onclick: load(async () => {
        const p = await api.getEvaluationPrompt(g, c);
        return [h("div", { class: "muted small" }, p.note),
          ...p.requests.flatMap((r, i) => [
            h("div", { class: "small" }, h("strong", {}, `Запрос ${i + 1} из ${p.requests.length}`), ` · критерии: ${r.criteria.join(", ")}`),
            h("div", { class: "small" }, "System"), pre(r.system),
            h("div", { class: "small" }, "User"), pre(r.user),
            h("div", { class: "small" }, "JSON-схема ответа"), pre(r.schema),
          ])];
      }) }, "Промпт")),
    out);
}

function answerText(a) {
  if (a === null || a === undefined) return "«требует проверки»";
  return { yes: "Да", no: "Нет", na: "N/A" }[a] || a;
}

function appealBlock(c, v, ctx) {
  const mine = ctx.appeals.filter((a) => a.criterion_id === c.id);
  const open = mine.find((a) => a.status === "open");
  const resolved = mine.filter((a) => a.status === "resolved");
  const out = [];
  for (const a of resolved) {
    out.push(h("div", { class: `appeal-note ${a.decision === "changed" ? "appeal-changed" : ""}` },
      h("a", { href: `#/a/${a.id}` }, `Апелляция #${a.id}`), ": ",
      a.decision === "changed"
        ? `изменено ${answerText(a.original_answer)} → ${answerText(a.new_answer)}${a.new_points ? ` (${a.original_points ?? "—"} → ${a.new_points} б.)` : ""}`
        : "оценка подтверждена",
      h("div", { class: "muted small" }, `Причина: ${a.reason}`)));
  }
  if (open) {
    out.push(h("div", { class: "appeal-note appeal-open" },
      h("a", { href: `#/a/${open.id}` }, `Апелляция #${open.id}`), " на рассмотрении: ",
      h("span", { class: "muted" }, `«${open.comment}»`)));
  } else if (ctx.role === "controller" && v && ctx.evaluation.is_current) {
    const box = h("div", { class: "appeal-form", hidden: true });
    const btn = h("button", { class: "btn btn-sm btn-ghost appeal-btn", type: "button",
      onclick: () => { box.hidden = !box.hidden; if (!box.hidden) box.querySelector("textarea").focus(); } }, "Подать апелляцию");
    const text = h("textarea", { class: "input textarea", rows: "3", placeholder: "Почему оценка неверна? (обязательно)" });
    const name = h("input", { class: "input input-sm", placeholder: "Ваше имя (необязательно)", value: pref("name", "") });
    const send = h("button", { class: "btn btn-sm btn-primary", type: "button", onclick: async () => {
      if (!text.value.trim()) { text.focus(); toast("Комментарий обязателен", "error"); return; }
      send.disabled = true;
      try {
        setPref("name", name.value.trim() || null);
        const a = await api.createAppeal({ evaluation_id: ctx.evaluation.id, criterion_id: c.id,
          comment: text.value.trim(), submitter_label: name.value.trim() || null });
        toast(`Апелляция #${a.id} подана`, "ok");
        document.dispatchEvent(new CustomEvent("appeals-changed"));
        ctx.onChanged && ctx.onChanged();
      } catch (e) { toast(e.message, "error"); send.disabled = false; }
    } }, "Отправить");
    box.append(text, h("div", { class: "appeal-form-row" }, name, send));
    out.push(btn, box);
  }
  return out;
}

function metaRow(label, value, extra) {
  return [h("dt", {}, label), h("dd", {}, value, extra ? " " : "", extra)];
}

function grossBadge(score, verdicts, transcript, flagLabels) {
  const ids = score.gross_criteria || [];
  const ev = ids.flatMap((id) => (verdicts.get(id)?.evidence || []));
  return h("div", { class: "gross-alert" },
    h("strong", {}, "Грубая ошибка"),
    h("span", {}, (score.gross_types || []).map((g) => flagLabels[g] || GROSS[g] || g).join(", ") ||
      (score.gross_criteria || []).join(", ")),
    ev.length ? h("button", { class: "link", type: "button", onclick: () => transcript.focus(ev.map((e) => e.message_id), { play: true }) },
      "показать в диалоге") : null);
}

function answerChip(c, v, row) {
  if (row?.status === "na_channel") return h("span", { class: "chip chip-na" }, "N/A");
  if (!v || v.status === "needs_review") return h("span", { class: "chip chip-review" }, "Требует проверки");
  const a = v.answer;
  let cls = "chip-na";
  if (c.type === "flag") cls = a === "yes" ? "chip-bad" : "chip-good";
  else if (a === "yes") cls = "chip-good";
  else if (a === "no") cls = "chip-bad";
  else if (a !== "na") {
    const max = Math.max(...c.scale.map((s) => Number(s.value)));
    cls = Number(a) === max ? "chip-good" : Number(a) === 0 ? "chip-bad" : "chip-mid";
  }
  return h("span", { class: `chip ${cls}` }, answerLabel(a, c.type));
}

function pointsLine(c, row) {
  if (c.type === "flag") return h("div", { class: "muted small" }, "Флаг: баллов не даёт и итог не обнуляет");
  if (!row) return null;
  if (row.status === "scored") {
    return h("div", { class: "points" },
      h("span", { class: "pts" }, `${row.points_display} из ${c.weight}`),
      h("span", { class: "muted small" }, ` · вклад в итог ${row.effective_points_display} из 100 (вес ${c.weight} → ${row.effective_weight_display})`));
  }
  if (row.status === "na" || row.status === "na_channel") {
    return h("div", { class: "muted small" }, `Вес ${c.weight} исключён — перераспределён на применимые критерии`);
  }
  if (row.status === "pending") return h("div", { class: "muted small" }, `Вес ${c.weight} не учтён до ручной проверки`);
  return null;
}

function criterionCard(c, v, row, messages, transcript, ctx) {
  const low = v && v.status === "ok" && v.confidence !== null && v.confidence !== undefined && v.confidence < LOW_CONFIDENCE;
  const split = v && v.votes && v.agreement !== null && v.agreement < 1;
  const classes = ["crit"];
  if (!v || v.status === "needs_review") classes.push("crit-review");
  if (low || split) classes.push("crit-low");
  if (c.type === "flag" && v?.answer === "yes") classes.push("crit-gross");

  const stepLabel = c.type === "scale" && v?.answer && v.answer !== "na"
    ? c.scale.find((s) => String(s.value) === String(v.answer))?.label : null;

  const evidence = (v?.evidence || []).map((e) => {
    const m = messages.get(e.message_id);
    const ts = m ? m.timestamp : e.timestamp;
    return h("button", { class: "evidence", type: "button", title: "Перейти к реплике",
      onclick: () => transcript.focus([e.message_id], { play: true }) },
    h("span", { class: "ev-ref", title: ts || "" }, `${e.message_id} · ${/\dT\d/.test(ts || "") && e.offset_sec !== null && e.offset_sec !== undefined ? fmtSec(e.offset_sec) : ts || ""}`),
    h("span", { class: "ev-quote" }, `«${e.quote}»`));
  });

  const body = [
    stepLabel ? h("div", { class: "step-label" }, `Ступень ${v.answer}: ${stepLabel}`) : null,
    v?.rationale ? h("p", { class: "rationale" }, v.rationale) : null,
    evidence.length ? h("div", { class: "evidence-list" }, evidence) : null,
    v?.checked_range ? h("button", { class: "evidence evidence-range", type: "button",
      onclick: () => transcript.focusRange(v.checked_range.from, v.checked_range.to) },
    h("span", { class: "ev-ref" }, `Проверено ${v.checked_range.from}–${v.checked_range.to}`),
    h("span", { class: "ev-quote" }, "действие не найдено в этом диапазоне")) : null,
    v?.na_reason ? h("div", { class: "na-reason" }, `N/A: ${v.na_reason}`) : null,
    row?.status === "na_channel" ? h("div", { class: "na-reason" }, `N/A: ${row.na_reason}`) : null,
    v?.status === "needs_review" ? h("div", { class: "alert alert-warn small" },
      v.review_reason || "Требует ручной проверки",
      (v.validation_errors || []).length ? h("ul", { class: "errors" }, v.validation_errors.map((e) => h("li", {}, e))) : null) : null,
    c.type === "flag" && v?.gross_types?.length ? h("div", { class: "gross-types" }, v.gross_types.map((g) => ctx.flagLabels[g] || GROSS[g] || g).join(", ")) : null,
    appealBlock(c, v, ctx),
  ];

  return h("article", { class: classes.join(" "), id: `crit-${c.id}` },
    h("header", { class: "crit-head" },
      h("div", {},
        h("span", { class: "crit-code" }, c.code || c.id), " ",
        h("span", { class: "crit-title" }, c.title)),
      answerChip(c, v, row)),
    pointsLine(c, row),
    body,
    split ? h("div", { class: "votes" }, `Проходы модели разошлись: ${Object.entries(v.votes).map(([a, n]) => `${answerLabel(a, c.type)} ×${n}`).join(", ")} — взят ответ большинства. Стоит проверить.`) : null,
    h("footer", { class: "crit-foot muted small" },
      v?.source === "cache" ? "из кэша" : v?.source === "rule" ? "правило без LLM" : v?.source === "appeal" ? "решение проверяющего" : "",
      v?.confidence !== null && v?.confidence !== undefined
        ? h("span", { class: low ? "low-conf" : "" }, ` уверенность ${Math.round(v.confidence * 100)}%${low ? " — низкая" : ""}`) : null,
      v?.samples > 1 && !split ? ` · консенсус ${v.samples}/${v.samples}` : null));
}
