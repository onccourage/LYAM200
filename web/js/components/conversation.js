// Карточка диалога: слева транскрипт, справа результат оценки.
import { api } from "../api.js";
import { clear, errorBox, h, spinner, toast } from "../dom.js";
import { CHANNEL } from "../labels.js";
import { activeChecklist, samplesPref } from "../state.js";
import { createEvaluationPanel } from "./evaluation.js";
import { createTranscript } from "./transcript.js";

export async function renderConversation(root, { group, conv, focus }) {
  clear(root).append(spinner());
  let conversation, evaluation = null;
  try {
    conversation = await api.getConversation(group, conv);
  } catch (e) {
    clear(root).append(errorBox(e));
    return;
  }
  try {
    evaluation = await api.getEvaluation(group, conv);
  } catch (e) {
    if (e.status !== 404) toast(e.message, "error");
  }

  // latest — активный чек-лист (им будем переоценивать), usedChecklist — версия, которой оценён диалог
  const checklistId = activeChecklist();
  let usedChecklist = null, latest = null;
  try {
    latest = (await api.getChecklist(checklistId)).checklist;
    usedChecklist = evaluation && (evaluation.checklist_id !== latest.id || evaluation.checklist_version !== latest.version)
      ? (await api.getChecklist(evaluation.checklist_id, evaluation.checklist_version)).checklist : latest;
  } catch (e) {
    toast(e.message, "error");
  }

  const transcript = createTranscript(conversation);
  const right = h("div", { class: "right-col" });

  const head = h("div", { class: "conv-head" },
    h("a", { class: "back", href: `#/d/${encodeURIComponent(group)}` }, "← К набору ", group),
    h("h1", { class: "page-title" }, conversation.conversation_id),
    h("div", { class: "conv-tags" },
      h("span", { class: "chip" }, CHANNEL[conversation.channel] || conversation.channel),
      h("span", { class: "chip" }, conversation.language || "язык не указан"),
      conversation.audio_url ? h("span", { class: "chip" }, "есть запись") : null));

  clear(root).append(h("div", { class: "page page-wide" }, head,
    h("div", { class: "conv-grid" }, transcript.el, right)));

  async function reevaluate({ full }) {
    const busy = h("div", { class: "overlay" }, spinner(full ? "Полная переоценка…" : "Оцениваю…"));
    right.append(busy);
    try {
      const r = await api.evaluate(group, conv, { checklistId: latest?.id || checklistId, full, samples: samplesPref() });
      const note = r.llm_criteria.length
        ? `в LLM: ${r.llm_criteria.join(", ")}` : "всё из кэша, без вызова LLM";
      toast(`Оценено: ${r.total ?? "не рассчитывается"} (${note})`, "ok");
      renderConversation(root, { group, conv });
    } catch (e) {
      busy.remove();
      toast(e.message, "error");
    }
  }

  if (evaluation && usedChecklist) {
    right.append(createEvaluationPanel({
      evaluation, checklist: usedChecklist, active: latest, conversation, transcript,
      onReevaluate: reevaluate,
      onChanged: () => renderConversation(root, { group, conv }),
    }));
  } else {
    right.append(h("section", { class: "panel eval-panel" },
      h("div", { class: "panel-head" }, h("h2", {}, "Оценка")),
      h("div", { class: "empty" },
        h("p", {}, "Диалог ещё не оценён."),
        latest ? h("p", { class: "muted small" }, `Будет использован чек-лист «${latest.name}» v${latest.version}.`) : null,
        h("button", { class: "btn btn-primary", type: "button", onclick: () => reevaluate({ full: false }) }, "Оценить"))));
  }

  if (focus) requestAnimationFrame(() => transcript.focus([focus]));
}
