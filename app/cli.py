"""Maintenance commands: python -m app.cli seed-demo / import-stock."""
import argparse
from pathlib import Path
from .database import init_db, SessionLocal
from .importer import import_rows, rows_from_file
from .replace_inventory import (checked_file_data, plan_replace, replace_stock,
                                backup_local_database, ReplaceError)

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description='PharmDesk — управление локальной базой')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('seed-demo', help='Загрузить учебный демонстрационный ящик №101')
    loader = commands.add_parser('import-stock', help='Импортировать CSV/XLSX экспорт одного ящика')
    loader.add_argument('file', help='Путь к файлу')
    loader.add_argument('--drawer', type=int, required=True, help='Номер ящика')
    loader.add_argument('--drawer-name', default='Без названия', help='Категория или название ящика')
    replacing = commands.add_parser(
        'replace-inventory',
        help='Replace drawers mentioned in an approved export, with mandatory DB backup',
    )
    replacing.add_argument('file', help='Path to reviewed CSV or XLSX')
    replacing.add_argument(
        '--confirm', default='',
        help='Required literal REPLACE (destructive only for drawers in this file)'
    )
    args = parser.parse_args()
    init_db()
    with SessionLocal() as db:
        try:
            if args.command == 'replace-inventory':
                raw = checked_file_data(args.file)
                plan = plan_replace(db, raw, args.file)
                summary = plan['summary']
                print()
                print('INVENTORY REPLACEMENT PREVIEW')
                print('Drawers from file:', summary['drawer_count'])
                print('Old stock rows to remove:', summary['existing_stock_rows_to_replace'])
                print('Reviewed stock rows to load:', summary['new_stock_rows'])
                print('Drawers NOT listed in file are unchanged.')
                print('This does NOT delete the reference medicine catalogue or audit history.')
                if args.confirm != 'REPLACE':
                    print('No changes made. To proceed, add: --confirm REPLACE')
                    return
                backup = backup_local_database()
                print('SQLite backup saved:', backup)
                result = replace_stock(db, raw, args.file)
                print('REPLACEMENT COMPLETE: loaded', result['new_stock_rows'])
                return
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
