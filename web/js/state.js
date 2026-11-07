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
