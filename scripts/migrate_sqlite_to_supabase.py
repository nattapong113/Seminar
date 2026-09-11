"""คัดลอกข้อมูลทั้งหมดจากไฟล์ SQLite เดิม (pattaya_tourism.db) ขึ้น Supabase ครั้งเดียว

- เก็บ id เดิมไว้ทุกแถว ความสัมพันธ์ระหว่างตาราง (imports -> data_sources ฯลฯ) จึงถูกต้องเหมือนเดิม
- ทำทั้งหมดใน transaction เดียว ถ้าพังกลางทางหรือจำนวนแถวไม่ตรง จะไม่มีอะไรถูกเขียนลง Supabase เลย
- ถ้า Supabase มีข้อมูลอยู่แล้วจะไม่ทำอะไร ต้องใส่ --replace ถึงจะลบของเดิมแล้วคัดลอกใหม่
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from psycopg import sql

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import SQLITE_BACKUP_PATH, TABLES, connect, initialize_database

TIMESTAMP_COLUMNS = {"imported_at", "created_at", "timestamp"}


def convert(column: str, value: Any) -> Any:
    if value is None:
        return None
    if column in TIMESTAMP_COLUMNS:
        # SQLite เก็บ CURRENT_TIMESTAMP เป็นข้อความเวลา UTC โดยไม่ระบุเขตเวลา
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    if column == "date":
        return date.fromisoformat(value)
    return value


def count_rows(connection, table: str) -> int:
    return connection.execute(
        sql.SQL("SELECT COUNT(*) AS total FROM {}").format(sql.Identifier(table))
    ).fetchone()["total"]


def main() -> None:
    parser = argparse.ArgumentParser(description="คัดลอกข้อมูลจาก SQLite เดิมขึ้น Supabase")
    parser.add_argument("--sqlite", type=Path, default=SQLITE_BACKUP_PATH, help="ไฟล์ SQLite ต้นทาง")
    parser.add_argument("--replace", action="store_true", help="ลบข้อมูลที่มีอยู่แล้วบน Supabase ก่อนคัดลอก")
    args = parser.parse_args()
    if not args.sqlite.exists():
        raise SystemExit(f"ไม่พบไฟล์: {args.sqlite}")

    initialize_database()
    source = sqlite3.connect(args.sqlite)
    source.row_factory = sqlite3.Row
    source_tables = {row["name"] for row in source.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    report: list[tuple[str, int, int]] = []
    try:
        with connect() as target:
            existing = {table: count_rows(target, table) for table in TABLES}
            if any(existing.values()):
                if not args.replace:
                    filled = ", ".join(f"{table} ({total})" for table, total in existing.items() if total)
                    raise SystemExit(f"Supabase มีข้อมูลอยู่แล้ว: {filled}\nถ้าต้องการลบแล้วคัดลอกใหม่ ให้รันซ้ำพร้อม --replace")
                target.execute(
                    sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                        sql.SQL(", ").join(map(sql.Identifier, TABLES))
                    )
                )

            for table in TABLES:
                if table not in source_tables:
                    report.append((table, 0, count_rows(target, table)))
                    continue
                target_columns = {
                    row["column_name"]
                    for row in target.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s",
                        (table,),
                    )
                }
                columns = [row["name"] for row in source.execute(f"PRAGMA table_info({table})") if row["name"] in target_columns]
                quoted = ", ".join(f'"{column}"' for column in columns)
                rows = source.execute(f"SELECT {quoted} FROM {table} ORDER BY 1").fetchall()

                with target.cursor() as cursor:
                    copy_statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns))
                    )
                    with cursor.copy(copy_statement) as copy:
                        for row in rows:
                            copy.write_row([convert(column, row[column]) for column in columns])

                if "id" in columns:
                    # ใส่ id เองตอนคัดลอก ต้องเลื่อนตัวนับ id ให้ต่อจากค่าสูงสุด ไม่งั้นแถวใหม่จะชน id เดิม
                    target.execute(
                        sql.SQL(
                            "SELECT setval(pg_get_serial_sequence(%s, 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {}"
                        ).format(sql.Identifier(table)),
                        (table,),
                    )
                report.append((table, len(rows), count_rows(target, table)))

            mismatched = [item for item in report if item[1] != item[2]]
            if mismatched:
                raise RuntimeError(f"จำนวนแถวไม่ตรงกัน ยกเลิกทั้งหมด: {mismatched}")
    finally:
        source.close()

    width = max(len(table) for table in TABLES)
    print(f"{'ตาราง'.ljust(width)}  SQLite  Supabase")
    for table, source_total, target_total in report:
        print(f"{table.ljust(width)}  {source_total:>6}  {target_total:>8}")
    print(f"\nคัดลอกครบ {sum(item[2] for item in report):,} แถว และเปิด Row Level Security ทุกตารางแล้ว")


if __name__ == "__main__":
    main()
