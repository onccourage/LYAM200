"""Загрузить набор из API организаторов в кэш и БД: python scripts/fetch_dataset.py DEV"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.dataset import DatasetError, load_dataset  # noqa: E402


def main() -> int:
    group_id = sys.argv[1] if len(sys.argv) > 1 else "DEV"
    db.init_db()
    try:
        r = load_dataset(group_id)
    except DatasetError as e:
        print(f"ОШИБКА: {e}")
        return 1
    print(f"{r.group_id}: {len(r.conversations)} диалогов (источник: {r.source})")
    for w in r.warnings:
        print(f"  ! {w}")
    by = {}
    for c in r.conversations:
        by[(c.channel, c.language)] = by.get((c.channel, c.language), 0) + 1
    for (ch, lang), n in sorted(by.items()):
        print(f"  {ch:12} {lang:3} {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
