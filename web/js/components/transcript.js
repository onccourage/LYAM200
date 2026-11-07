// Транскрипт с якорями #m{N}, подсветкой доказательств/диапазонов и плеером для звонков.
import { API_BASE_URL } from "../api.js";
import { fmtSec, h } from "../dom.js";
import { SPEAKER } from "../labels.js";

export function createTranscript(conversation) {
  const { messages, audio_url: audioUrl } = conversation;
  const byId = new Map();
  let player = null;

  const list = h("ol", { class: "transcript", "aria-label": "Транскрипт" });
  for (const m of messages) {
    const role = SPEAKER[m.speaker] ? m.speaker : "other";
    const playBtn = audioUrl && m.offset_sec !== null
      ? h("button", { class: "btn-icon", type: "button", title: `Воспроизвести с ${fmtSec(m.offset_sec)}`,
        onclick: (e) => { e.stopPropagation(); player.seekMessage(m); } }, "▶")
      : null;
    const li = h("li", { id: m.id, class: `msg role-${role}`, tabindex: "-1" },
      h("div", { class: "msg-meta" },
        h("a", { class: "msg-id", href: `#${m.id}`, title: "Ссылка на реплику",
          onclick: (e) => { e.preventDefault(); api.focus([m.id]); } }, m.id),
        h("span", { class: "msg-role" }, SPEAKER[m.speaker] || m.speaker),
        tsLabel(m),
        playBtn),
      h("div", { class: "msg-text" }, m.text));
    byId.set(m.id, li);
    list.append(li);
  }

  if (audioUrl) player = createPlayer(conversation);

  const el = h("section", { class: "panel transcript-panel" },
    h("div", { class: "panel-head" }, h("h2", {}, "Транскрипт"), h("span", { class: "muted" }, `${messages.length} реплик`)),
    player ? player.el : null,
    h("div", { class: "transcript-scroll" }, list));

  function clearMarks() {
    for (const li of byId.values()) li.classList.remove("hl-evidence", "hl-range", "hl-flash");
  }

  const api = {
    el,
    player,
    /** Подсветить реплики и прокрутить к первой. kind: evidence | range */
    focus(ids, { kind = "evidence", play = false } = {}) {
      clearMarks();
      const found = ids.map((id) => byId.get(id)).filter(Boolean);
      if (!found.length) return;
      for (const li of found) li.classList.add(kind === "range" ? "hl-range" : "hl-evidence");
      const first = found[0];
      first.scrollIntoView({ behavior: "smooth", block: "center" });
      first.classList.add("hl-flash");
      first.focus({ preventScroll: true });
      if (history.replaceState) {
        const base = location.hash.replace(/\/m\d+$/, "");
        history.replaceState(null, "", `${base}/${first.id}`);
      }
      if (play && player) {
        const msg = messages.find((m) => m.id === first.id);
        if (msg && msg.offset_sec !== null) player.seekMessage(msg);
      }
    },
    focusRange(fromId, toId) {
      const a = Number(fromId.slice(1)), b = Number(toId.slice(1));
      const ids = messages.filter((m) => { const n = Number(m.id.slice(1)); return n >= a && n <= b; }).map((m) => m.id);
      api.focus(ids, { kind: "range" });
    },
    clear: clearMarks,
  };
  return api;
}

// Для звонков (ISO-время) показываем смещение от начала, исходный timestamp — во всплывающей подсказке.
function tsLabel(m) {
  const iso = /\dT\d/.test(m.timestamp || "");
  if (iso && m.offset_sec !== null) {
    return h("span", { class: "msg-ts", title: `исходный timestamp: ${m.timestamp}` }, fmtSec(m.offset_sec));
  }
  return h("span", { class: "msg-ts", title: m.offset_sec !== null ? `смещение ${fmtSec(m.offset_sec)}` : "" }, m.timestamp);
}

/**
 * Перевод смещения реплики (offset_sec от первого события транскрипта) во время внутри записи.
 *
 * Проверено на 5 звонках DEV (сопоставление речи в аудио с репликами):
 *  - timestamp реплики звонка — это КОНЕЦ фразы, а не начало;
 *  - запись начинается позже первого события транскрипта на 9–30 с, и этот сдвиг почти точно равен
 *    (последнее смещение транскрипта − длительность записи) + ~2 с (ошибка < 1 с на всех звонках).
 * Поэтому: время в записи ≈ offset − сдвиг − оценка длительности фразы. Поправлять — только здесь.
 */
export function audioTimeFor(message, messages, duration) {
  const offs = messages.map((m) => m.offset_sec).filter((x) => x !== null && x !== undefined);
  const last = offs.length ? Math.max(...offs) : 0;
  const shift = Number.isFinite(duration) && duration > 0 ? Math.max(0, last - duration + 2) : 0;
  const idx = messages.indexOf(message);
  const prev = messages.slice(0, idx).reverse().find((m) => m.offset_sec !== null && m.offset_sec !== undefined);
  const speechLen = Math.max(1.5, Math.min(20, (message.text || "").length / 14));  // ~14 символов/с речи
  let start = message.offset_sec - speechLen;
  if (prev && prev.offset_sec > start) start = prev.offset_sec;     // фраза не начинается раньше конца предыдущей
  const t = start - shift - 0.5;                                    // небольшой запас
  return Math.max(0, Number.isFinite(duration) && duration > 0 ? Math.min(t, duration - 1) : t);
}

function createPlayer(conversation) {
  const { audio_url: pageUrl, messages } = conversation;
  const src = `${API_BASE_URL}/api/audio/${encodeURIComponent(conversation.group_id)}/${encodeURIComponent(conversation.conversation_id)}`;
  const audio = h("audio", { controls: true, preload: "metadata", src });
  const status = h("span", { class: "muted small" });
  const openLink = h("a", { class: "btn btn-sm", href: pageUrl, target: "_blank", rel: "noopener",
    title: "Страница записи у организаторов" }, "Открыть запись");
  const fallback = h("div", { class: "alert alert-warn small", hidden: true },
    "Запись не удаётся воспроизвести во встроенном плеере. Откройте её в новой вкладке и перемотайте к тайм-коду.");

  audio.addEventListener("error", () => { fallback.hidden = false; });

  const el = h("div", { class: "player" }, audio, h("div", { class: "player-row" }, openLink, status), fallback);

  function playAt(t, label) {
    status.textContent = `Тайм-код в записи: ${fmtSec(t)}${label ? ` (${label})` : ""}`;
    try {
      audio.currentTime = t;
      const p = audio.play();
      if (p && p.catch) p.catch(() => { fallback.hidden = false; });
    } catch {
      fallback.hidden = false;
    }
  }

  return {
    el,
    seekMessage(m) {
      const go = () => playAt(audioTimeFor(m, messages, audio.duration), m.id);
      if (Number.isFinite(audio.duration) && audio.duration > 0) go();
      else {
        status.textContent = "Загружаю запись…";
        audio.addEventListener("loadedmetadata", go, { once: true });
        if (audio.readyState === 0) audio.load();
      }
    },
    seek(offset) { playAt(offset); },
  };
}
