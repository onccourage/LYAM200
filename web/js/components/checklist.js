// Редактор чек-листа: критерии, секции, веса, шкалы, N/A, каналы, аудио, guidance, типы нарушений.
// Сохранение = новая неизменяемая версия. Проверка и сумма весов — на бэкенде (живой предпросмотр).
import { api } from "../api.js";
import { clear, errorBox, fmtDate, h, spinner, toast } from "../dom.js";
import { CHANNEL } from "../labels.js";
import { activeChecklist, pref, setPref } from "../state.js";

const TYPES = { binary: "Да / Нет", scale: "Шкала", flag: "Флаг нарушения (без баллов)" };
const CHANGE = { added: "добавлен", removed: "удалён", modified: "изменён", disabled: "выключен", enabled: "включён" };
const FIELD = { title: "название", description: "описание", type: "тип", weight: "вес", na_allowed: "N/A", na_condition: "условие N/A",
  scale: "шкала", flag_types: "типы нарушений", guidance: "правила", channels: "каналы", requires_audio: "аудио",
  section: "секция", code: "код", enabled: "включён" };
const clone = (x) => JSON.parse(JSON.stringify(x));

export async function renderChecklistEditor(root, { id, version } = {}) {
  clear(root).append(spinner());
  const checklistId = id || activeChecklist();
  let list, loaded, versions;
  try {
    [list, loaded, versions] = await Promise.all([
      api.listChecklists(), api.getChecklist(checklistId, version), api.checklistVersions(checklistId)]);
  } catch (e) {
    clear(root).append(errorBox(e));
    return;
  }
  const latestVersion = Math.max(...versions.map((v) => v.version));
  const editor = new Editor(root, loaded.checklist, { list, versions, latestVersion });
  editor.render();
}

class Editor {
  constructor(root, checklist, meta) {
    this.root = root;
    this.base = clone(checklist);           // версия, от которой редактируем
    this.draft = clone(checklist);
    this.meta = meta;
    this.savedIds = new Set(checklist.criteria.map((c) => c.id));  // ID сохранённых критериев не меняются
    this.validation = { warnings: [], errors: [], total_weight: null };
    this.lastDiff = null;
    this.timer = null;
    this.side = h("aside", { class: "editor-side" });
  }

  // ---------------------------------------------------------------- общий каркас
  render() {
    const { list, versions, latestVersion } = this.meta;
    const d = this.draft;
    const pickList = h("select", { class: "input input-sm", "aria-label": "Чек-лист",
      onchange: (e) => (location.hash = `#/checklist/${encodeURIComponent(e.target.value)}`) },
    list.map((c) => h("option", { value: c.checklist_id, selected: c.checklist_id === d.id }, `${c.name} (${c.checklist_id})`)));
    const pickVer = h("select", { class: "input input-sm", "aria-label": "Версия",
      onchange: (e) => (location.hash = `#/checklist/${encodeURIComponent(d.id)}/${e.target.value}`) },
    [...versions].reverse().map((v) => h("option", { value: v.version, selected: v.version === this.base.version },
      `v${v.version} · ${fmtDate(v.created_at)}${v.version === latestVersion ? " (последняя)" : ""}`)));
    const importInput = h("input", { type: "file", accept: ".json,application/json", class: "visually-hidden", id: "cl-import",
      onchange: (e) => this.importFile(e.target.files[0]) });

    const head = h("div", { class: "editor-head" },
      h("h1", { class: "page-title" }, "Редактор чек-листа"),
      h("div", { class: "toolbar" }, pickList, pickVer,
        h("label", { class: "btn btn-sm", for: "cl-import" }, "Импорт JSON"), importInput,
        h("button", { class: "btn btn-sm", type: "button", onclick: () => this.exportJson() }, "Экспорт JSON"),
        this.base.version !== latestVersion ? h("span", { class: "chip chip-partial" },
          `открыта v${this.base.version}; сохранение создаст v${latestVersion + 1}`) : null,
        activeChecklist() === d.id ? h("span", { class: "chip chip-done" }, "используется для оценки")
          : h("button", { class: "btn btn-sm", type: "button", onclick: () => { setPref("checklist", d.id); toast(`Для оценки выбран «${d.name}»`, "ok"); this.render(); } },
            "Использовать для оценки")));

    this.main = h("div", { class: "editor-main" });
    this.drawMain();
    clear(this.root).append(h("div", { class: "page page-wide" }, head, h("div", { class: "editor-grid" }, this.main, this.side)));
    this.validate(true);
  }

  drawMain() {
    const d = this.draft;
    const general = h("section", { class: "panel panel-stack" },
      h("h2", {}, "Общее"),
      field("Название", input(d.name, (v) => { d.name = v; this.changed(); })),
      field("Описание (общие правила, видны в README/экспорте)", textarea(d.description || "", (v) => { d.description = v; this.changed(); }, 2)),
      h("div", { class: "row-fields" },
        field("ID чек-листа", h("input", { class: "input", value: d.id, disabled: true })),
        field("Знаков округления итога", numberInput(d.rounding, (v) => { d.rounding = v; this.changed(); }, { min: 0, max: 6, step: 1 }))));

    const sections = h("section", { class: "panel panel-stack" },
      h("div", { class: "panel-head" }, h("h2", {}, "Секции"),
        h("button", { class: "btn btn-sm", type: "button", onclick: () => this.addSection() }, "+ Секция")),
      [...d.sections].sort((a, b) => a.order - b.order).map((s) => this.sectionRow(s)));

    const order = [...d.sections].sort((a, b) => a.order - b.order).map((s) => s.id);
    const crits = h("section", { class: "panel panel-stack" },
      h("div", { class: "panel-head" }, h("h2", {}, `Критерии (${d.criteria.length})`),
        h("button", { class: "btn btn-sm btn-primary", type: "button", onclick: () => this.addCriterion() }, "+ Критерий")),
      order.map((sid) => {
        const cs = d.criteria.filter((c) => c.section === sid);
        const sec = d.sections.find((s) => s.id === sid);
        return h("div", { class: "crit-group" }, h("h3", { class: "crit-group-title" }, sec.title || sid),
          cs.length ? cs.map((c) => this.criterionCard(c)) : h("div", { class: "muted small" }, "Нет критериев"));
      }));
    this.main.replaceChildren(general, sections, crits);
  }

  // ---------------------------------------------------------------- секции
  sectionRow(s) {
    const used = this.draft.criteria.some((c) => c.section === s.id);
    const isNew = !this.base.sections.some((x) => x.id === s.id);
    return h("div", { class: "section-edit" },
      h("input", { class: "input input-sm mono", value: s.id, disabled: !isNew, "aria-label": "ID секции", title: "ID секции",
        onchange: (e) => { const v = e.target.value.trim(); if (!v) return; for (const c of this.draft.criteria) if (c.section === s.id) c.section = v; s.id = v; this.changed(true); } }),
      input(s.title, (v) => { s.title = v; this.changed(); }, { placeholder: "Название секции", class: "input input-sm grow" }),
      numberInput(s.order, (v) => { s.order = v; this.changed(true); }, { step: 1, class: "input input-sm w-num", title: "Порядок" }),
      h("button", { class: "btn btn-sm btn-ghost", type: "button", disabled: used,
        title: used ? "В секции есть критерии — сначала перенесите или удалите их" : "Удалить секцию",
        onclick: () => { this.draft.sections = this.draft.sections.filter((x) => x !== s); this.changed(true); } }, "✕"));
  }

  addSection() {
    const ids = new Set(this.draft.sections.map((s) => s.id));
    let n = this.draft.sections.length + 1;
    while (ids.has(`section_${n}`)) n++;
    const order = Math.max(0, ...this.draft.sections.map((s) => s.order)) + 1;
    this.draft.sections.push({ id: `section_${n}`, title: "Новая секция", order });
    this.changed(true);
  }

  // ---------------------------------------------------------------- критерии
  addCriterion(sectionId) {
    const ids = new Set(this.draft.criteria.map((c) => c.id));
    const nums = this.draft.criteria.map((c) => Number((c.id.match(/\d+$/) || [0])[0]));
    let n = Math.max(0, ...nums) + 1;
    while (ids.has(`c${String(n).padStart(2, "0")}`)) n++;
    const id = `c${String(n).padStart(2, "0")}`;
    const section = sectionId || [...this.draft.sections].sort((a, b) => a.order - b.order)[0]?.id;
    this.draft.criteria.push({ id, code: id.slice(1), section, title: "Новый критерий", description: "", type: "binary",
      weight: 10, na_allowed: false, na_condition: "", scale: [], flag_types: [], guidance: "",
      channels: ["agent chat", "voice call"], requires_audio: false, enabled: true });
    this.openId = id;
    this.changed(true);
    requestAnimationFrame(() => document.getElementById(`edit-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" }));
  }

  setType(c, type) {
    if (type === c.type) return;
    c.type = type;
    if (type === "scale" && !c.scale.length) {
      const top = Number(c.weight) || 10;
      c.scale = [{ value: top, label: "" }, { value: 0, label: "" }];
    }
    if (type !== "scale") c.scale = [];
    if (type === "flag") c.weight = 0;
    else c.flag_types = [];
    if (type !== "flag" && !Number(c.weight)) c.weight = 10;
    this.changed(true);
  }

  criterionCard(c) {
    const isNew = !this.savedIds.has(c.id);
    const baseC = this.base.criteria.find((x) => x.id === c.id);
    const dirty = !baseC || JSON.stringify(baseC) !== JSON.stringify(c);
    const open = this.openId === c.id;
    const summary = [TYPES[c.type], c.type === "flag" ? null : `вес ${c.weight}`,
      c.type === "scale" ? `ступени ${c.scale.map((s) => s.value).join("/")}` : null,
      c.na_allowed ? "N/A" : null, c.channels.length === 1 ? CHANNEL[c.channels[0]] || c.channels[0] : null,
      c.requires_audio ? "аудио" : null, c.enabled ? null : "выключен"].filter(Boolean).join(" · ");

    const body = h("div", { class: "crit-edit-body", hidden: !open });
    const card = h("article", { class: `crit crit-edit ${dirty ? "crit-dirty" : ""} ${c.enabled ? "" : "crit-off"}`, id: `edit-${c.id}` },
      h("header", { class: "crit-head clickable", onclick: () => { this.openId = open ? null : c.id; this.drawMain(); } },
        h("div", {},
          h("span", { class: "crit-code" }, c.id), " ",
          h("span", { class: "crit-title" }, c.title || "(без названия)"),
          h("div", { class: "muted small" }, summary)),
        h("div", { class: "crit-head-right" },
          dirty ? h("span", { class: "chip chip-partial" }, isNew ? "новый" : "изменён") : null,
          h("span", { class: "muted" }, open ? "▲" : "▼"))),
      body);
    if (open) body.append(...this.criterionFields(c, isNew));
    return card;
  }

  criterionFields(c, isNew) {
    const out = [];
    out.push(h("div", { class: "row-fields" },
      field("ID (стабильный)", h("input", { class: "input mono", value: c.id, disabled: !isNew,
        title: isNew ? "Можно изменить до сохранения" : "ID сохранённого критерия не меняется — история и кэш привязаны к нему",
        onchange: (e) => { const v = e.target.value.trim(); if (v && !this.draft.criteria.some((x) => x !== c && x.id === v)) { if (this.openId === c.id) this.openId = v; c.id = v; this.changed(true); } else { e.target.value = c.id; toast("ID пустой или уже занят", "error"); } } })),
      field("Код", input(c.code, (v) => { c.code = v; this.changed(); })),
      field("Секция", h("select", { class: "input", onchange: (e) => { c.section = e.target.value; this.changed(true); } },
        this.draft.sections.map((s) => h("option", { value: s.id, selected: s.id === c.section }, s.title || s.id)))),
      field("Включён", h("label", { class: "check" }, h("input", { type: "checkbox", checked: c.enabled,
        onchange: (e) => { c.enabled = e.target.checked; this.changed(true); } }), " участвует в оценке"))));
    out.push(field("Название", input(c.title, (v) => { c.title = v; this.changed(); })));
    out.push(field("Что оценивать (описание / правило «Да»)", textarea(c.description, (v) => { c.description = v; this.changed(); }, 2)));
    out.push(h("div", { class: "row-fields" },
      field("Тип", h("select", { class: "input", onchange: (e) => this.setType(c, e.target.value) },
        Object.entries(TYPES).map(([k, t]) => h("option", { value: k, selected: c.type === k }, t)))),
      field("Вес", numberInput(c.weight, (v) => { c.weight = v; this.changed(); },
        { min: 0, step: "any", disabled: c.type === "flag", title: c.type === "flag" ? "У флага вес всегда 0" : "" }))));

    if (c.type === "scale") out.push(this.scaleEditor(c));
    if (c.type === "flag") out.push(this.flagTypesEditor(c));

    const naCond = textarea(c.na_condition, (v) => { c.na_condition = v; this.changed(); }, 2);
    naCond.disabled = !c.na_allowed;
    out.push(h("div", { class: "subpanel" },
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: c.na_allowed,
        onchange: (e) => { c.na_allowed = e.target.checked; naCond.disabled = !c.na_allowed; this.changed(); } }), " N/A разрешён"),
      field("Условие неприменимости", naCond)));

    out.push(h("div", { class: "row-fields" },
      field("Каналы", h("div", { class: "checks" }, Object.entries(CHANNEL).map(([k, t]) =>
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: c.channels.includes(k),
          onchange: (e) => { c.channels = e.target.checked ? [...new Set([...c.channels, k])] : c.channels.filter((x) => x !== k); this.changed(); } }), ` ${t}`)))),
      field("Аудио", h("label", { class: "check", title: "Дикция, интонация и т.п.: по тексту не оценивается — N/A без записи или ручная проверка" },
        h("input", { type: "checkbox", checked: c.requires_audio, onchange: (e) => { c.requires_audio = e.target.checked; this.changed(); } }),
        " требует прослушивания"))));
    out.push(field("Правила оценки для модели (guidance)", textarea(c.guidance, (v) => { c.guidance = v; this.changed(); }, 4)));
    out.push(h("div", { class: "actions" },
      h("button", { class: "btn btn-sm", type: "button", onclick: () => this.move(c, -1) }, "↑"),
      h("button", { class: "btn btn-sm", type: "button", onclick: () => this.move(c, 1) }, "↓"),
      h("button", { class: "btn btn-sm btn-danger", type: "button", onclick: () => {
        if (!confirm(`Удалить критерий ${c.id} «${c.title}»? В новой версии его не будет.`)) return;
        this.draft.criteria = this.draft.criteria.filter((x) => x !== c);
        this.changed(true);
      } }, "Удалить критерий")));
    return out;
  }

  scaleEditor(c) {
    const rows = c.scale.map((s, i) => h("div", { class: "step-row" },
      numberInput(s.value, (v) => { s.value = v; this.changed(); }, { step: "any", min: 0, class: "input input-sm w-num", "aria-label": "Значение ступени" }),
      input(s.label, (v) => { s.label = v; this.changed(); }, { class: "input input-sm grow", placeholder: "Описание ступени" }),
      h("button", { class: "btn btn-sm btn-ghost", type: "button", disabled: c.scale.length <= 2, title: "Удалить ступень",
        onclick: () => { c.scale.splice(i, 1); this.changed(true); } }, "✕")));
    return h("div", { class: "subpanel" },
      h("div", { class: "field-label" }, "Ступени шкалы (верхняя = полный вес, баллы = вес × ступень / верхняя)"),
      rows,
      h("button", { class: "btn btn-sm", type: "button", onclick: () => { c.scale.push({ value: 0, label: "" }); this.changed(true); } }, "+ Ступень"));
  }

  flagTypesEditor(c) {
    const rows = c.flag_types.map((f, i) => h("div", { class: "step-row" },
      input(f.id, (v) => { f.id = v.trim(); this.changed(); }, { class: "input input-sm mono w-id", placeholder: "id" }),
      input(f.label, (v) => { f.label = v; this.changed(); }, { class: "input input-sm grow", placeholder: "Описание типа нарушения" }),
      h("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => { c.flag_types.splice(i, 1); this.changed(true); } }, "✕")));
    return h("div", { class: "subpanel" },
      h("div", { class: "field-label" }, "Типы нарушения (необязательно; модель укажет, какие из них есть)"),
      rows,
      h("button", { class: "btn btn-sm", type: "button", onclick: () => { c.flag_types.push({ id: `type_${c.flag_types.length + 1}`, label: "" }); this.changed(true); } }, "+ Тип"));
  }

  move(c, dir) {
    const sameSec = this.draft.criteria.filter((x) => x.section === c.section);
    const idx = sameSec.indexOf(c);
    const other = sameSec[idx + dir];
    if (!other) return;
    const all = this.draft.criteria;
    const a = all.indexOf(c), b = all.indexOf(other);
    [all[a], all[b]] = [all[b], all[a]];
    this.changed(true);
  }

  // ---------------------------------------------------------------- изменения, проверка, сохранение
  changed(rerender = false) {
    this.lastDiff = null;
    if (rerender) this.drawMain();
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.validate(), 350);
    this.drawSide();
  }

  isDirty() {
    const strip = (x) => JSON.stringify({ ...x, version: 0, created_at: null });
    return strip(this.draft) !== strip(this.base);
  }

  async validate(initial = false) {
    try {
      const r = await api.validateChecklist(this.draft);
      this.validation = { warnings: r.warnings, errors: [], total_weight: r.total_weight };
    } catch (e) {
      this.validation = { warnings: [], errors: e.detail?.errors || [e.message], total_weight: null };
    }
    if (initial || this.side.isConnected) this.drawSide();
  }

  drawSide() {
    const v = this.validation;
    const dirty = this.isDirty();
    const latest = this.meta.latestVersion;
    const saveBtn = h("button", { class: "btn btn-primary", type: "button", disabled: !dirty || v.errors.length > 0,
      onclick: () => this.save() }, `Сохранить как новую версию (v${latest + 1})`);
    const weightOk = v.total_weight === 100;
    this.side.replaceChildren(h("div", { class: "panel panel-stack sticky-side" },
      h("div", { class: "stat" },
        h("div", { class: `stat-value ${v.total_weight === null ? "" : weightOk ? "ok" : "warn"}` }, v.total_weight === null ? "—" : String(v.total_weight)),
        h("div", { class: "stat-label" }, "сумма весов включённых критериев (итог всё равно нормируется к 100)")),
      v.errors.length ? h("div", { class: "alert alert-error small" }, h("strong", {}, "Нельзя сохранить:"),
        h("ul", { class: "errors" }, v.errors.map((e) => h("li", {}, e)))) : null,
      v.warnings.length ? h("div", { class: "alert alert-warn small" }, h("ul", { class: "errors" }, v.warnings.map((w) => h("li", {}, w)))) : null,
      !v.errors.length && !v.warnings.length ? h("div", { class: "muted small" }, "Ошибок и предупреждений нет") : null,
      saveBtn,
      dirty ? h("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => { if (confirm("Отменить все несохранённые изменения?")) { this.draft = clone(this.base); this.changed(true); } } }, "Отменить изменения") : null,
      h("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => this.saveAsNew() }, "Сохранить как новый чек-лист…"),
      this.lastDiff ? this.applyPanel() : null));
  }

  async save() {
    try {
      const r = await api.saveChecklist(this.draft);
      const saved = r.checklist;
      const diff = await api.checklistDiff(saved.id, this.base.version, saved.version);
      toast(`Сохранено: ${saved.name} v${saved.version}`, "ok");
      this.meta.versions = await api.checklistVersions(saved.id);
      this.meta.latestVersion = saved.version;
      this.base = clone(saved);
      this.draft = clone(saved);
      this.savedIds = new Set(saved.criteria.map((c) => c.id));
      setPref("checklist", saved.id);
      this.lastDiff = diff;
      if (history.replaceState) history.replaceState(null, "", `#/checklist/${encodeURIComponent(saved.id)}/${saved.version}`);
      this.render();
      this.lastDiff = diff;
      this.drawSide();
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async saveAsNew() {
    const id = prompt("ID нового чек-листа (латиница, цифры, _ -):", `${this.draft.id}_copy`);
    if (!id) return;
    const name = prompt("Название:", `${this.draft.name} (копия)`);
    if (!name) return;
    try {
      const r = await api.saveChecklist({ ...this.draft, id: id.trim(), name: name.trim() });
      toast(`Создан чек-лист ${r.checklist.id} v${r.checklist.version}`, "ok");
      setPref("checklist", r.checklist.id);
      location.hash = `#/checklist/${encodeURIComponent(r.checklist.id)}`;
    } catch (e) {
      toast(e.message, "error");
    }
  }

  applyPanel() {
    const d = this.lastDiff;
    const llm = d.changes.filter((c) => c.llm);
    const group = h("input", { class: "input input-sm", value: pref("group") || "DEV", "aria-label": "group_id" });
    return h("div", { class: "apply" },
      h("h3", { class: "crit-group-title" }, `Изменения v${d.from_version} → v${d.to_version}`),
      d.changes.length ? h("ul", { class: "errors small" }, d.changes.map((c) => h("li", {},
        h("strong", {}, c.criterion_id), ` ${CHANGE[c.change]}`,
        c.fields.length ? ` (${c.fields.map((f) => FIELD[f] || f).join(", ")})` : "",
        h("span", { class: c.llm ? "chip chip-partial" : "chip chip-na" }, c.llm ? "переоценка LLM" : "только пересчёт")))) : h("div", { class: "muted small" }, "Критерии не менялись"),
      h("div", { class: "muted small" }, llm.length
        ? `При применении в модель уйдут только: ${llm.map((c) => c.criterion_id).join(", ")}. Остальное — из кэша.`
        : "Вызов модели не понадобится — только пересчёт баллов."),
      h("div", { class: "apply-row" }, group,
        h("button", { class: "btn btn-primary btn-sm", type: "button", onclick: async () => {
          const g = group.value.trim();
          if (!g) return;
          try {
            const job = await api.startBatch({ group_id: g, checklist_id: this.base.id, checklist_version: this.base.version });
            setPref("group", g);
            toast(`Применяю v${this.base.version} к набору ${g}: задание #${job.id}`, "ok");
            location.hash = `#/d/${encodeURIComponent(g)}`;
          } catch (e) { toast(e.message, "error"); }
        } }, "Применить ко всему набору")),
      h("a", { class: "small", href: `#/d/${encodeURIComponent(group.value || "DEV")}`, onclick: () => setPref("checklist", this.base.id) },
        "…или выбрать диалоги на экране набора"));
  }

  // ---------------------------------------------------------------- импорт / экспорт
  async importFile(file) {
    if (!file) return;
    let data;
    try {
      data = JSON.parse(await file.text());
    } catch (e) {
      toast(`Не JSON: ${e.message}`, "error");
      return;
    }
    const cl = data.checklist || data;
    if (!cl.criteria || !cl.sections) { toast("В файле нет sections/criteria", "error"); return; }
    if (cl.id && cl.id !== this.draft.id &&
      !confirm(`В файле чек-лист «${cl.id}», открыт «${this.draft.id}». Загрузить содержимое как новую версию «${this.draft.id}»? ` +
        "(Чтобы создать отдельный чек-лист, загрузите и нажмите «Сохранить как новый чек-лист…»)")) return;
    cl.id = this.draft.id;
    this.draft = { rounding: 2, description: "", ...cl };
    for (const c of this.draft.criteria) {
      c.scale = c.scale || []; c.flag_types = c.flag_types || []; c.channels = c.channels || ["agent chat", "voice call"];
      c.na_condition = c.na_condition || ""; c.guidance = c.guidance || ""; c.description = c.description || ""; c.code = c.code || "";
      if (c.enabled === undefined) c.enabled = true;
    }
    toast("Импортировано в редактор — проверьте и сохраните как новую версию", "ok");
    this.changed(true);
  }

  exportJson() {
    const blob = new Blob([JSON.stringify(this.draft, null, 2)], { type: "application/json" });
    const a = h("a", { href: URL.createObjectURL(blob), download: `${this.draft.id}_v${this.base.version}${this.isDirty() ? "_draft" : ""}.json` });
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
  }
}

// ---------------------------------------------------------------- поля формы
function field(label, control) {
  return h("label", { class: "field" }, h("span", { class: "field-label" }, label), control);
}

function input(value, onInput, attrs = {}) {
  return h("input", { class: "input", value: value ?? "", ...attrs, oninput: (e) => onInput(e.target.value) });
}

function textarea(value, onInput, rows = 3) {
  const t = h("textarea", { class: "input textarea", rows: String(rows), oninput: (e) => onInput(e.target.value) });
  t.value = value ?? "";
  return t;
}

function numberInput(value, onInput, attrs = {}) {
  return h("input", { class: "input w-num", type: "number", value: value ?? 0, ...attrs,
    oninput: (e) => { if (e.target.value !== "" && !isNaN(Number(e.target.value))) onInput(Number(e.target.value)); } });
}
