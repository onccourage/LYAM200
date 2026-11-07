// Панель результата: итог, секции, грубая ошибка, метаданные, критерии с доказательствами.
// Все числа — строки, посчитанные бэкендом (scoring.py). Фронтенд ничего не округляет и не считает.
import { fmtDate, fmtSec, h } from "../dom.js";
import { GROSS, LOW_CONFIDENCE, answerLabel } from "../labels.js";

export function createEvaluationPanel({ evaluation, checklist, latestVersion, conversation, transcript, onReevaluate }) {
  const score = evaluation.score || {};
  const rows = new Map((score.criteria || []).map((c) => [c.criterion_id, c]));
  const verdicts = new Map((evaluation.answers || []).map((v) => [v.criterion_id, v]));
  const messages = new Map(conversation.messages.map((m) => [m.id, m]));
  const outdated = latestVersion && latestVersion > evaluation.checklist_version;

  // --- итог
  const total = evaluation.total;
  const pending = score.pending || [];
  const summary = h("div", { class: "summary" },
    h("div", { class: "total" },
      h("div", { class: "total-value" }, total ?? "—"),
      h("div", { class: "total-label" }, total === null || total === undefined ? "не рассчитывается: нет применимых критериев" : "из 100")),
    h("div", { class: "summary-side" },
      score.gross_error ? grossBadge(score, verdicts, transcript) : null,
      pending.length ? h("div", { class: "alert alert-warn small" },
        `Требуют ручной проверки: ${pending.join(", ")}. Итог посчитан без них.`) : null,
      h("dl", { class: "meta" },
        metaRow("Чек-лист", `${checklist.name} · v${evaluation.checklist_version}`,
          outdated ? h("span", { class: "chip chip-partial" }, `есть v${latestVersion}`) : null),
        metaRow("Модель", `${evaluation.model} · промпт ${evaluation.prompt_version}`),
        metaRow("Стоимость", `$${evaluation.cost} · вызовов LLM: ${evaluation.llm_calls} · токены ${evaluation.tokens_in}/${evaluation.tokens_out}`),
        metaRow("Оценено", fmtDate(evaluation.created_at))),
      h("div", { class: "actions" },
        h("button", { class: `btn ${outdated ? "btn-primary" : ""}`, type: "button", onclick: () => onReevaluate({ full: false }),
          title: "Оценить последней версией чек-листа; неизменённые критерии берутся из кэша" },
        "Переоценить текущей версией"),
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
      cs.map((c) => criterionCard(c, verdicts.get(c.id), rows.get(c.id), messages, transcript)))));

  return h("section", { class: "panel eval-panel" },
    h("div", { class: "panel-head" }, h("h2", {}, "Оценка")),
    summary, sections, criteriaEl);
}

function metaRow(label, value, extra) {
  return [h("dt", {}, label), h("dd", {}, value, extra ? " " : "", extra)];
}

function grossBadge(score, verdicts, transcript) {
  const ids = score.gross_criteria || [];
  const ev = ids.flatMap((id) => (verdicts.get(id)?.evidence || []));
  return h("div", { class: "gross-alert" },
    h("strong", {}, "Грубая ошибка"),
    h("span", {}, (score.gross_types || []).map((g) => GROSS[g] || g).join(", ") || "тип не указан"),
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

function criterionCard(c, v, row, messages, transcript) {
  const low = v && v.status === "ok" && v.confidence !== null && v.confidence !== undefined && v.confidence < LOW_CONFIDENCE;
  const classes = ["crit"];
  if (!v || v.status === "needs_review") classes.push("crit-review");
  if (low) classes.push("crit-low");
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
    c.type === "flag" && v?.gross_types?.length ? h("div", { class: "gross-types" }, v.gross_types.map((g) => GROSS[g] || g).join(", ")) : null,
  ];

  return h("article", { class: classes.join(" "), id: `crit-${c.id}` },
    h("header", { class: "crit-head" },
      h("div", {},
        h("span", { class: "crit-code" }, c.code || c.id), " ",
        h("span", { class: "crit-title" }, c.title)),
      answerChip(c, v, row)),
    pointsLine(c, row),
    body,
    h("footer", { class: "crit-foot muted small" },
      v?.source === "cache" ? "из кэша" : v?.source === "rule" ? "правило без LLM" : v?.source === "appeal" ? "по апелляции" : "",
      v?.confidence !== null && v?.confidence !== undefined
        ? h("span", { class: low ? "low-conf" : "" }, ` уверенность ${Math.round(v.confidence * 100)}%${low ? " — низкая" : ""}`) : null));
}
