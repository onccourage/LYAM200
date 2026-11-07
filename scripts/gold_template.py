"""Шаблоны эталонной разметки: python scripts/gold_template.py DEV [ids...] [--suggest 18] [--from-model]

Создаёт data/gold/{conversation_id}.json с пустыми ответами (null), подсказкой допустимых значений
по каждому критерию и транскриптом для чтения. Существующие файлы не перезаписываются.
--from-model подставляет ответы текущей оценки как черновик (отмечается "draft_from_model": true —
размечающий обязан проверить каждый ответ, иначе эталон смещён в пользу модели).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.gold import suggest  # noqa: E402
from app.prompt_builder import answer_options  # noqa: E402
from app.scoring import applies_to_channel  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group_id")
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--suggest", type=int, default=18, help="сколько диалогов подобрать, если ids не заданы")
    ap.add_argument("--checklist", default="default_chat")
    ap.add_argument("--from-model", action="store_true")
    ap.add_argument("--out", default=str(settings.data_dir / "gold"))
    a = ap.parse_args()

    db.init_db()
    db.seed_checklists()
    cl = db.get_checklist(a.checklist)
    convs = db.list_conversations(a.group_id)
    if not convs:
        sys.exit(f"набор {a.group_id} не загружен")
    by_id = {c.conversation_id: c for c in convs}
    targets = [by_id[i] for i in a.ids] if a.ids else suggest(convs, a.suggest)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for c in targets:
        f = out / f"{c.conversation_id}.json"
        if f.exists():
            print(f"  = {f.name} уже есть, пропуск")
            continue
        crit = [x for x in cl.enabled_criteria if applies_to_channel(x, c.channel)]
        answers = {x.id: None for x in crit}
        draft = False
        if a.from_model:
            ev = db.get_current_evaluation(a.group_id, c.conversation_id)
            if ev:
                draft = True
                for v in ev["answers"]:
                    if v["criterion_id"] in answers and v.get("status") == "ok":
                        answers[v["criterion_id"]] = ({"answer": v["answer"], "gross_types": v.get("gross_types", [])}
                                                      if v.get("gross_types") else v["answer"])
        doc = {
            "conversation_id": c.conversation_id, "group_id": a.group_id, "annotator": "",
            "checklist_id": cl.id, "checklist_version": cl.version, "draft_from_model": draft,
            "answers": answers,
            "notes": {},
            "_help": {x.id: f"{x.title}: {', '.join(answer_options(x))}" +
                      (f" (flag: gross_types из {x.flag_type_ids})" if x.flag_types else "") for x in crit},
            "_meta": {"channel": c.channel, "language": c.language, "audio_url": c.audio_url},
            "_transcript": [f"[{m.id}][{m.timestamp}][{m.speaker}] {m.text}" for m in c.messages],
        }
        f.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  + {f.name}  ({c.channel}, {c.language}, {len(c.messages)} реплик)")
    print(f"Готово. Заполните answers (null → значение) и annotator, затем: python scripts/eval_gold.py {a.group_id}")


if __name__ == "__main__":
    main()
