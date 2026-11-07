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
