// Маленький набор линейных иконок взамен голых Unicode-символов (▲✕♪▶ и т.п.) — они дёшево
// выглядят в корпоративном продукте. Только примитивы SVG (line/polyline/polygon/circle) с точными
// координатами — не кривые Безье от руки. currentColor — иконка наследует цвет текста/кнопки.
const SVG_NS = "http://www.w3.org/2000/svg";

function svg(viewBox, children, attrs = {}) {
  const el = document.createElementNS(SVG_NS, "svg");
  el.setAttribute("viewBox", viewBox);
  el.setAttribute("width", attrs.size || 16);
  el.setAttribute("height", attrs.size || 16);
  el.setAttribute("fill", "none");
  el.setAttribute("stroke", "currentColor");
  el.setAttribute("stroke-width", attrs.strokeWidth || 2);
  el.setAttribute("stroke-linecap", "round");
  el.setAttribute("stroke-linejoin", "round");
  el.setAttribute("aria-hidden", "true");
  el.classList.add("icon");
  if (attrs.className) el.classList.add(...attrs.className.split(" "));
  for (const c of children) el.append(c);
  return el;
}

function shape(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

export function iconChevron(opts = {}) {
  return svg("0 0 24 24", [shape("polyline", { points: "6 9 12 15 18 9" })], opts);
}

export function iconX(opts = {}) {
  return svg("0 0 24 24", [
    shape("line", { x1: 18, y1: 6, x2: 6, y2: 18 }),
    shape("line", { x1: 6, y1: 6, x2: 18, y2: 18 }),
  ], opts);
}

export function iconPlus(opts = {}) {
  return svg("0 0 24 24", [
    shape("line", { x1: 12, y1: 5, x2: 12, y2: 19 }),
    shape("line", { x1: 5, y1: 12, x2: 19, y2: 12 }),
  ], opts);
}

export function iconArrowLeft(opts = {}) {
  return svg("0 0 24 24", [
    shape("line", { x1: 19, y1: 12, x2: 5, y2: 12 }),
    shape("polyline", { points: "12 19 5 12 12 5" }),
  ], opts);
}

export function iconPlay(opts = {}) {
  const el = svg("0 0 24 24", [shape("polygon", { points: "6 4 20 12 6 20" })], opts);
  el.setAttribute("fill", "currentColor");
  el.setAttribute("stroke", "none");
  return el;
}

export function iconAlertTriangle(opts = {}) {
  return svg("0 0 24 24", [
    shape("polygon", { points: "12 3 22 20 2 20" }),
    shape("line", { x1: 12, y1: 9, x2: 12, y2: 13 }),
    shape("circle", { cx: 12, cy: 17, r: 1, fill: "currentColor", stroke: "none" }),
  ], opts);
}

export function iconSun(opts = {}) {
  return svg("0 0 24 24", [
    shape("circle", { cx: 12, cy: 12, r: 4 }),
    shape("line", { x1: 12, y1: 2, x2: 12, y2: 5 }),
    shape("line", { x1: 12, y1: 19, x2: 12, y2: 22 }),
    shape("line", { x1: 2, y1: 12, x2: 5, y2: 12 }),
    shape("line", { x1: 19, y1: 12, x2: 22, y2: 12 }),
    shape("line", { x1: 4.5, y1: 4.5, x2: 6.6, y2: 6.6 }),
    shape("line", { x1: 17.4, y1: 17.4, x2: 19.5, y2: 19.5 }),
    shape("line", { x1: 4.5, y1: 19.5, x2: 6.6, y2: 17.4 }),
    shape("line", { x1: 17.4, y1: 6.6, x2: 19.5, y2: 4.5 }),
  ], opts);
}

let moonMaskSeq = 0;

export function iconMoon(opts = {}) {
  // Серп месяца — только точная геометрия окружностей (без кривых от руки): большой круг
  // r=9 в центре, из него маской вычитается смещённый круг r=7 — ровно классический приём.
  const id = `moon-mask-${++moonMaskSeq}`;
  const el = svg("0 0 24 24", [], opts);
  el.setAttribute("fill", "currentColor");
  el.setAttribute("stroke", "none");
  const mask = shape("mask", { id });
  mask.append(
    shape("rect", { x: 0, y: 0, width: 24, height: 24, fill: "white" }),
    shape("circle", { cx: 15, cy: 9, r: 7, fill: "black" }),
  );
  el.append(mask, shape("circle", { cx: 12, cy: 12, r: 9, mask: `url(#${id})` }));
  return el;
}
