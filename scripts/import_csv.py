from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys
import sqlite3

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import get_connection, initialize_database


def read_csv(path: Path) -> list[dict[str, str]]:
    encodings = ("utf-8-sig", "cp874", "tis-620")
    last_error: UnicodeDecodeError | None = None
    for encoding in encodings:
        try:
            with path.open("r", encoding=encoding, newline="") as file:
                return list(csv.DictReader(file))
        except UnicodeDecodeError as error:
            last_error = error
    raise ValueError(f"อ่านไฟล์ {path.name} ไม่ได้: {last_error}")


def integer(value: str | None) -> int:
    return int(float((value or "0").replace(",", "").strip()))


def clean(value: str | None) -> str:
    return (value or "").strip()


def import_monthly(path: Path, rows: list[dict[str, str]], connection: sqlite3.Connection) -> int:
    location = "พัทยา ชลบุรี"
    connection.execute("DELETE FROM tourism_monthly WHERE source_file = ?", (path.name,))
    values = []
    for row in rows:
        year_be = integer(row.get("calendar_year"))
        values.append((
            path.name,
            location,
            year_be,
            year_be - 543,
            integer(row.get("month_id")),
            clean(row.get("month_name")) or None,
            integer(row.get("number_of_tourists")),
            clean(row.get("unit")) or "คน",
        ))
    connection.executemany(
        """
        INSERT INTO tourism_monthly
            (source_file, location, calendar_year_be, calendar_year, month_id,
             month_name, number_of_tourists, unit)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    return len(values)


def import_demographics(path: Path, rows: list[dict[str, str]], connection: sqlite3.Connection) -> int:
    location = "พัทยา ชลบุรี"
    connection.execute("DELETE FROM demographic_profiles WHERE source_file = ?", (path.name,))
    values = []
    dimensions = (
        ("gender", "เพศ"),
        ("age", "อายุ"),
        ("income", "รายได้"),
        ("education", "การศึกษา"),
        ("occupation", "อาชีพ"),
    )
    for row in rows:
        year_be = integer(row.get("year"))
        population = integer(row.get("population"))
        for column, category in dimensions:
            segment = clean(row.get(column))
            if segment:
                values.append((path.name, location, year_be, year_be - 543, category, segment, population, clean(row.get("unit")) or "คน"))
                break
    connection.executemany(
        """
        INSERT INTO demographic_profiles
            (source_file, location, calendar_year_be, calendar_year, category,
             segment, population, unit)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    return len(values)


def main() -> None:
    parser = argparse.ArgumentParser(description="นำเข้า CSV ข้อมูลท่องเที่ยวและประชากร")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    initialize_database()
    connection = get_connection()
    result: list[str] = []
    try:
        for path in args.files:
            if not path.exists():
                raise SystemExit(f"ไม่พบไฟล์: {path}")
            rows = read_csv(path)
            headers = set(rows[0]) if rows else set()
            if {"calendar_year", "month_id", "number_of_tourists"}.issubset(headers):
                count = import_monthly(path, rows, connection)
                result.append(f"{path.name}: นักท่องเที่ยวรายเดือน {count} แถว")
            elif {"year", "population"}.issubset(headers):
                count = import_demographics(path, rows, connection)
                result.append(f"{path.name}: ประชากรจำแนกกลุ่ม {count} แถว")
            else:
                raise SystemExit(f"รูปแบบไฟล์ไม่รองรับ: {path.name}")
        connection.commit()
    finally:
        connection.close()
    print("\n".join(result))


if __name__ == "__main__":
    main()
