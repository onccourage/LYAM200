// Только пользовательские удобства (тема, последний набор). Источник истины — бэкенд.
const KEY = "cqa.";

export function pref(name, fallback = null) {
  try {
    const v = localStorage.getItem(KEY + name);
    return v === null ? fallback : v;
  } catch {
    return fallback;
  }
}

export function setPref(name, value) {
  try {
    if (value === null || value === undefined) localStorage.removeItem(KEY + name);
    else localStorage.setItem(KEY + name, value);
  } catch { /* приватный режим и т.п. */ }
}

export const runtime = { config: null };

// Роль без авторизации: «Контролёр» подаёт апелляции, «Проверяющий» их рассматривает.
export const ROLES = { controller: "Контролёр", reviewer: "Проверяющий" };
export function role() {
  const r = pref("role", "controller");
  return ROLES[r] ? r : "controller";
}

// Чек-лист, которым оцениваем (последняя версия). Выбирается в редакторе / на экране набора.
export function activeChecklist() {
  return pref("checklist", "default_chat");
}

// Число проходов модели на диалог (консенсус). 1 — обычная оценка.
export function samplesPref() {
  const n = Number(pref("samples", "1"));
  return Number.isInteger(n) && n >= 1 && n <= 7 ? n : 1;
}

// Профиль модели (переключатель провайдера в UI — "default" или id из LLM_PROFILE_N_* в .env).
// Список профилей известен только после загрузки /api/config (runtime.config.llm_profiles).
export function providerPref() {
  return pref("provider", "default");
}
