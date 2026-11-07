// Пакетная оценка: запуск (выбранные / весь набор), прогресс с опросом бэкенда, отмена, продолжение.
import { api } from "../api.js";
import { h, toast } from "../dom.js";
import { activeChecklist, providerPref, runtime, samplesPref, setPref } from "../state.js";

const POLL_MS = 1000;
const ACTIVE = new Set(["queued", "running"]);
const STATUS = { queued: "в очереди", running: "выполняется", done: "завершено", cancelled: "отменено",
  interrupted: "прервано (перезапуск сервера)", failed: "остановлено: ошибка провайдера" };

export function createBatchPanel({ group, getSelected, onProgress, onFinished }) {
  const fullBox = h("input", { type: "checkbox", id: "batch-full" });
  const selBtn = h("button", { class: "btn", type: "button", disabled: true, onclick: () => start(getSelected()) }, "Оценить выбранные");
  const allBtn = h("button", { class: "btn btn-primary", type: "button", onclick: () => start(null) }, "Оценить весь набор");
  const progress = h("div", { class: "batch-progress", hidden: true });
  const clSelect = h("select", { class: "input input-sm", "aria-label": "Чек-лист для оценки",
    onchange: () => setPref("checklist", clSelect.value) });
  const samplesSel = h("select", { class: "input input-sm", "aria-label": "Проходов модели",
    title: "Консенсус: каждый диалог оценивается N раз, по каждому критерию берётся ответ большинства; расхождения подсвечиваются",
    onchange: () => setPref("samples", samplesSel.value) },
  [[1, "1 проход"], [3, "3 прохода (консенсус)"], [5, "5 проходов (консенсус)"]].map(([v, t]) =>
    h("option", { value: v, selected: samplesPref() === v }, t)));
  api.listChecklists().then((list) => {
    const cur = activeChecklist();
    clSelect.replaceChildren(...list.map((c) => h("option", { value: c.checklist_id, selected: c.checklist_id === cur },
      `${c.name} · v${c.latest_version}`)));
  }).catch(() => {});

  // Переключатель профиля модели («разнообразие подходов»: облачная/локальная модель и т.п.) — только
  // если в .env настроен хотя бы один дополнительный профиль (LLM_PROFILE_2_*); иначе не загромождаем UI.
  const profiles = runtime.config?.llm_profiles || [];
  const providerSel = profiles.length > 1 ? h("select", { class: "input input-sm", "aria-label": "Модель",
    title: "Профиль модели — настраивается в .env (LLM_PROFILE_N_*), ключи не покидают сервер",
    onchange: () => setPref("provider", providerSel.value) },
  profiles.map((p) => h("option", { value: p.id, selected: p.id === providerPref() }, `${p.label} (${p.provider})`))) : null;
  let timer = null, lastProcessed = -1;

  const el = h("div", { class: "batch" },
    h("div", { class: "batch-controls" }, h("span", { class: "muted small" }, "Чек-лист:"), clSelect, samplesSel,
      providerSel, selBtn, allBtn,
      h("label", { class: "check", for: "batch-full", title: "Сбросить кэш критериев и заново спросить модель по всем критериям" },
        fullBox, " полностью, без кэша")),
    progress);

  async function start(ids) {
    const full = fullBox.checked;
    if (full && !confirm("Полная переоценка заново вызовет модель по всем критериям. Продолжить?")) return;
    try {
      const job = await api.startBatch({ group_id: group, conversation_ids: ids && ids.length ? ids : null, full,
        checklist_id: clSelect.value || activeChecklist(), samples: Number(samplesSel.value) || 1,
        provider: providerSel ? providerSel.value : providerPref() });
      toast(`Пакетная оценка #${job.id}: ${job.total} диалогов`, "ok");
      show(job);
      poll(job.id);
    } catch (e) {
      toast(e.message, "error");
    }
  }

  function show(job) {
    progress.hidden = false;
    const pct = job.total ? Math.round((job.processed / job.total) * 100) : 100;
    const active = ACTIVE.has(job.status);
    const resumable = !active && (["interrupted", "cancelled", "failed"].includes(job.status) || job.failed > 0);
    selBtn.disabled = active || !getSelected().length;
    allBtn.disabled = active;
    progress.replaceChildren(...[
      h("div", { class: "batch-line" },
        h("strong", {}, `Задание #${job.id}`),
        h("span", { class: `chip ${active ? "chip-partial" : job.status === "done" && !job.failed ? "chip-done" : "chip-bad"}` }, STATUS[job.status] || job.status),
        h("span", { class: "muted small" }, `${job.checklist_id} v${job.checklist_version}${job.full ? " · полностью" : ""}${job.samples > 1 ? ` · консенсус ×${job.samples}` : ""}${job.provider && job.provider !== "default" ? ` · модель: ${(profiles.find((p) => p.id === job.provider) || {}).label || job.provider}` : ""}`)),
      h("div", { class: "bar bar-lg", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(job.total), "aria-valuenow": String(job.processed) },
        h("div", { class: "bar-fill", style: `width:${pct}%` })),
      h("div", { class: "batch-line small" },
        h("span", {}, `${job.processed} из ${job.total}`),
        h("span", { class: "muted" }, `оценено ${job.succeeded} · требуют проверки ${job.partial} · пропущено (уже оценены) ${job.skipped} · ошибок ${job.failed}`),
        h("span", { class: "muted" }, `вызовов LLM ${job.llm_calls} · $${job.cost}`)),
      job.current?.length ? h("div", { class: "muted small" }, `Сейчас: ${job.current.join(", ")}`) : null,
      job.errors?.length ? h("details", { class: "small" }, h("summary", {}, `Ошибки (${job.errors.length})`),
        h("ul", { class: "errors" }, job.errors.map((e) => h("li", {}, `${e.conversation_id}: ${e.error}`)))) : null,
      h("div", { class: "actions" },
        active ? h("button", { class: "btn btn-sm", type: "button", onclick: () => cancel(job.id) }, "Остановить") : null,
        resumable ? h("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => resume(job.id) },
          "Продолжить (пропустит уже оценённые)") : null),
    ].filter(Boolean));
  }

  async function cancel(id) {
    try { await api.cancelBatch(id); toast("Останавливаю после текущих диалогов…"); } catch (e) { toast(e.message, "error"); }
  }

  async function resume(id) {
    try { show(await api.resumeBatch(id)); poll(id); } catch (e) { toast(e.message, "error"); }
  }

  function poll(id) {
    clearTimeout(timer);
    const tick = async () => {
      if (!el.isConnected) return;  // ушли с экрана
      let job;
      try { job = await api.getBatch(id); } catch (e) { toast(e.message, "error"); return; }
      show(job);
      if (job.processed !== lastProcessed) { lastProcessed = job.processed; onProgress && onProgress(job); }
      if (ACTIVE.has(job.status)) timer = setTimeout(tick, POLL_MS);
      else onFinished && onFinished(job);
    };
    timer = setTimeout(tick, 300);
  }

  // при открытии экрана — подхватить последнее задание набора (в т.ч. после перезагрузки страницы)
  api.listBatches(group, 1).then((jobs) => {
    const job = jobs[0];
    if (!job) return;
    if (ACTIVE.has(job.status)) { show(job); poll(job.id); }
    else if (job.status === "interrupted" || (job.status === "cancelled" && job.processed < job.total)) show(job);
  }).catch(() => {});

  return {
    el,
    selectionChanged(n) {
      selBtn.textContent = n ? `Оценить выбранные (${n})` : "Оценить выбранные";
      if (!allBtn.disabled) selBtn.disabled = !n;
    },
  };
}

export function createStats(group) {
  const el = h("div", { class: "stats" });
  async function refresh() {
    try {
      const s = await api.datasetStats(group);
      const tile = (label, value, hint) => h("div", { class: "stat", title: hint || "" },
        h("div", { class: "stat-value" }, value ?? "—"), h("div", { class: "stat-label" }, label));
      el.replaceChildren(
        tile("оценено", `${s.evaluated} / ${s.conversations}`),
        tile("средний итог", s.avg_total),
        tile("требуют проверки", s.needs_review),
        tile("грубые ошибки", s.gross_errors),
        tile("ср. стоимость диалога", s.avg_cost_per_conversation ? `$${s.avg_cost_per_conversation}` : null,
          "Сумма всех вызовов LLM по диалогу (включая ретраи и инкрементальные переоценки) / число оценённых диалогов"),
        tile("потрачено всего", `$${s.spent}`, `Оценок в истории: ${s.evaluations_total}; ср. токены на оценку с вызовом LLM: ${s.avg_tokens_in ?? "—"} / ${s.avg_tokens_out ?? "—"}`));
    } catch { /* статистика не критична */ }
  }
  refresh();
  return { el, refresh };
}
