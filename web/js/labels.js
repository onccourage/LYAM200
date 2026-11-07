// Подписи интерфейса для фиксированных значений протокола (не критериев: критерии приходят из чек-листа).
export const SPEAKER = { operator: "Оператор", client: "Клиент", bot: "Бот", system: "Система" };
export const CHANNEL = { "agent chat": "Чат", "voice call": "Звонок" };
export const GROSS = {
  insult: "оскорбление клиента",
  bypass_service: "платная услуга в обход компании",
  confidential_disclosure: "раскрытие конфиденциальной информации",
};
export const EVAL_STATUS = { done: "Оценён", partial: "Требует проверки", error: "Ошибка" };
export const LOW_CONFIDENCE = 0.7;

export function answerLabel(answer, type) {
  if (answer === null || answer === undefined) return "Требует проверки";
  if (answer === "na") return "N/A";
  if (type === "flag") return answer === "yes" ? "Нарушение" : "Нет нарушения";
  if (answer === "yes") return "Да";
  if (answer === "no") return "Нет";
  return answer;
}

export const DECISION = { confirmed: "Подтверждено", changed: "Изменено" };

/** Допустимые ответы критерия для формы решения: [value, label]. Берутся из конфигурации критерия. */
export function answerOptions(c) {
  let opts;
  if (c.type === "scale") opts = [...c.scale].sort((a, b) => b.value - a.value).map((s) => [String(s.value), `${s.value} — ${s.label}`]);
  else if (c.type === "flag") opts = [["yes", "Нарушение есть"], ["no", "Нарушения нет"]];
  else opts = [["yes", "Да"], ["no", "Нет"]];
  if (c.na_allowed) opts.push(["na", `N/A — ${c.na_condition || "неприменимо"}`]);
  return opts;
}
