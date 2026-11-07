"""Выгрузка результатов в формате сдачи (CQA_Student_Guide_Appendix_Results, JSON 1.0).

python scripts/export_results.py dev "Название команды"
python scripts/export_results.py prod "Название команды" --group CTRL-7

dev пишет в results/dev/results.json (ожидается 65 диалогов — 60 чатов + 5 звонков, но это не
проверяется жёстко: число диалогов в DEV окончательно известно только организаторам).
prod пишет в results/prod/results.json и ТРЕБУЕТ ровно 10 диалогов в наборе — иначе остановится
с ошибкой до того, как вы случайно закоммитите неполный или задвоенный файл.

Перед контрольным часом: при замене критерия в редакторе чек-листа правьте СУЩЕСТВУЮЩИЙ критерий
на нужном слоте (c01..c08), а не выключайте его и не добавляйте новый ID — иначе этот скрипт
остановится с объяснением, какой слот не найден.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.results_export import ExportError, build_results, write_results  # noqa: E402

REQUIRE_COUNT = {"dev": None, "prod": 10}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("kind", choices=["dev", "prod"])
    ap.add_argument("team_name", nargs="?", default=None,
                    help="по умолчанию берётся из переменной окружения TEAM_NAME")
    ap.add_argument("--group", help="group_id набора (по умолчанию: DEV для dev, иначе нужно указать)")
    ap.add_argument("--checklist", default="default_chat")
    ap.add_argument("--model-mode", default="external_api", choices=["local", "external_api", "hybrid"])
    ap.add_argument("--model-name", default=None, help="по умолчанию берётся из самих оценок")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    import os

    db.init_db()
    team_name = a.team_name or os.environ.get("TEAM_NAME")
    if not team_name:
        sys.exit("укажите название команды аргументом или переменной окружения TEAM_NAME в .env")
    group = a.group or ("DEV" if a.kind == "dev" else None)
    if not group:
        sys.exit("для prod укажите --group (group_id контрольного набора)")
    out = Path(a.out) if a.out else settings.data_dir.parent / "results" / a.kind / "results.json"

    try:
        result = build_results(group, team_name, model_mode=a.model_mode, model_name=a.model_name,
                               checklist_id=a.checklist, require_count=REQUIRE_COUNT[a.kind])
    except ExportError as e:
        print(f"ОШИБКА: {e}")
        return 1

    write_results(result, out)
    print(f"Записано: {out}")
    print(f"Диалогов: {result.ok_count} ok, {result.error_count} error, всего {result.ok_count + result.error_count}")
    for w in result.warnings:
        print(f"  ! [{w.conversation_id}] {w.message}")
    if result.error_count:
        print("\nДиалоги со статусом error (не скрывайте их, но проверьте, что это ожидаемо):")
        for r in result.payload["results"]:
            if r["status"] == "error":
                print(f"  - {r['conversation_id']}: {r['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
