// Транскрипт с якорями #m{N}, подсветкой доказательств/диапазонов и плеером для звонков.
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
        onclick: (e) => { e.stopPropagation(); player.seek(m.offset_sec); } }, "▶")
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

  if (audioUrl) player = createPlayer(audioUrl);

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
        if (msg && msg.offset_sec !== null) player.seek(msg.offset_sec);
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

function createPlayer(url) {
  const audio = h("audio", { controls: true, preload: "metadata", src: url });
  const status = h("span", { class: "muted small" });
  const openLink = h("a", { class: "btn btn-sm", href: url, target: "_blank", rel: "noopener" }, "Открыть запись");
  const fallback = h("div", { class: "alert alert-warn small", hidden: true },
    "Запись не удаётся воспроизвести во встроенном плеере. Откройте её в новой вкладке и перемотайте к тайм-коду.");
  let lastOffset = null;

  audio.addEventListener("error", () => { fallback.hidden = false; });

  const el = h("div", { class: "player" }, audio, h("div", { class: "player-row" }, openLink, status), fallback);

  return {
    el,
    seek(offset) {
      lastOffset = offset;
      status.textContent = `Тайм-код: ${fmtSec(offset)}`;
      openLink.href = `${url}#t=${Math.floor(offset)}`;
      try {
        audio.currentTime = offset;
        const p = audio.play();
        if (p && p.catch) p.catch(() => { fallback.hidden = false; });
      } catch {
        fallback.hidden = false;
      }
    },
    get lastOffset() { return lastOffset; },
  };
}
