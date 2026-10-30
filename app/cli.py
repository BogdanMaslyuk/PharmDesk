"""Maintenance commands: python -m app.cli seed-demo / import-stock."""
import argparse
from pathlib import Path
from .database import init_db, SessionLocal
from .importer import import_rows, rows_from_file

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description='PharmDesk — управление локальной базой')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('seed-demo', help='Загрузить учебный демонстрационный ящик №101')
    loader = commands.add_parser('import-stock', help='Импортировать CSV/XLSX экспорт одного ящика')
    loader.add_argument('file', help='Путь к файлу')
    loader.add_argument('--drawer', type=int, required=True, help='Номер ящика')
    loader.add_argument('--drawer-name', default='Без названия', help='Категория или название ящика')
    args = parser.parse_args()
    init_db()
    with SessionLocal() as db:
        try:
            if args.command == 'seed-demo':
                outcome = import_rows(db, rows_from_file(ROOT / 'examples' / 'demo_inventory.csv'), 101, 'Учебный ассортимент (вымышленные записи)', demo=True)
            else:
                outcome = import_rows(db, rows_from_file(args.file), args.drawer, args.drawer_name)
        except Exception:
            db.rollback()
            raise
        print(f"Добавлено: {outcome['created']}, обновлено: {outcome['updated']}, пропущено: {outcome['skipped']}")


if __name__ == '__main__':
    main()
