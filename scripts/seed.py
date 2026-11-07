"""Сид предрасчитанных оценок.

python scripts/seed.py export DEV   # текущие оценки набора → data/seed/DEV.json (закоммитить)
python scripts/seed.py import DEV   # data/seed/DEV.json → БД (демо без API модели)

Сервер сам импортирует все data/seed/*.json при старте, если в БД нет ни одной оценки (SEED_ON_START=1).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.seed import export_seed, import_seed, seed_dir  # noqa: E402


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in ("export", "import"):
        sys.exit(__doc__)
    cmd, group = sys.argv[1], sys.argv[2]
    db.init_db()
    db.seed_checklists()
    if cmd == "export":
        p = export_seed(group)
        print(f"Сид записан: {p}")
    else:
        print(import_seed(seed_dir() / f"{group}.json"))


if __name__ == "__main__":
    main()
