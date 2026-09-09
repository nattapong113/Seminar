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
from typing import Optional


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


def import_zone_traffic(path: Path, rows: list[dict[str, str]], connection: sqlite3.Connection) -> int:
    connection.execute("DELETE FROM zone_traffic_volume WHERE source_file = ?", (path.name,))
    values = []
    for row in rows:
        year_be = integer(row.get("calendar_year"))
        values.append((
            path.name,
            clean(row.get("intersection_id")),
            clean(row.get("intersection_name")),
            year_be,
            year_be - 543,
            integer(row.get("month_id")),
            clean(row.get("month_name")) or None,
            integer(row.get("vehicle_volume")),
            clean(row.get("unit")) or "คัน",
        ))
    connection.executemany(
        """
        INSERT INTO zone_traffic_volume
            (source_file, intersection_id, intersection_name, calendar_year_be, calendar_year,
             month_id, month_name, vehicle_volume, unit)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )
    return len(values)


def import_demographics_wide(path: Path, rows: list[dict[str, str]], connection: sqlite3.Connection) -> int:
    """รองรับไฟล์รูปแบบจริงของเทศบาลเมืองพัทยา: category_type,category_label,pop_2563,pop_2564,..."""
    location = "พัทยา ชลบุรี"
    connection.execute("DELETE FROM demographic_profiles WHERE source_file = ?", (path.name,))
    year_columns = [key for key in (rows[0].keys() if rows else []) if key.startswith("pop_")]
    values = []
    for row in rows:
        category = clean(row.get("category_type"))
        segment = clean(row.get("category_label"))
        if not category or not segment:
            continue
        for year_column in year_columns:
            raw_value = row.get(year_column)
            if not clean(raw_value):
                continue
            year_be = integer(year_column.removeprefix("pop_"))
            values.append((path.name, location, year_be, year_be - 543, category, segment, integer(raw_value), "คน"))
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


def ensure_data_source(connection: sqlite3.Connection, name: str, source_type: str, endpoint: Optional[str] = None, metadata: Optional[str] = None) -> int:
    cur = connection.execute("SELECT id FROM data_sources WHERE name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur = connection.execute(
        "INSERT INTO data_sources (name, source_type, endpoint, metadata) VALUES (?, ?, ?, ?)",
        (name, source_type, endpoint, metadata),
    )
    return cur.lastrowid


def create_import_record(connection: sqlite3.Connection, data_source_id: Optional[int], import_type: str, source_name: str, raw_filename: str, source_url: Optional[str], records_total: int) -> int:
    cur = connection.execute(
        "INSERT INTO imports (data_source_id, import_type, source_name, raw_filename, source_url, records_total, records_success, records_failed) VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
        (data_source_id, import_type, source_name, raw_filename, source_url, records_total),
    )
    return cur.lastrowid


def update_import_record(connection: sqlite3.Connection, import_id: int, success: int, failed: int, notes: Optional[str] = None) -> None:
    connection.execute(
        "UPDATE imports SET records_success = ?, records_failed = ?, notes = ? WHERE id = ?",
        (success, failed, notes, import_id),
    )


def record_import_error(connection: sqlite3.Connection, import_id: int, row_number: Optional[int], error_text: str) -> None:
    connection.execute(
        "INSERT INTO import_errors (import_id, row_number, error_text) VALUES (?, ?, ?)",
        (import_id, row_number, error_text),
    )


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
            total_rows = len(rows)
            # register data source and import record
            ds_name = path.stem
            data_source_id = ensure_data_source(connection, ds_name, "csv")
            import_id = create_import_record(connection, data_source_id, "csv", path.name, path.name, None, total_rows)
            headers = set(rows[0]) if rows else set()
            success_count = 0
            failed_count = 0
            if {"calendar_year", "month_id", "number_of_tourists"}.issubset(headers):
                try:
                    count = import_monthly(path, rows, connection)
                    success_count = count
                    result.append(f"{path.name}: นักท่องเที่ยวรายเดือน {count} แถว")
                except Exception as exc:
                    failed_count = total_rows
                    record_import_error(connection, import_id, None, str(exc))
                    result.append(f"{path.name}: นำเข้าไม่สำเร็จ - {exc}")
            elif {"year", "population"}.issubset(headers):
                try:
                    count = import_demographics(path, rows, connection)
                    success_count = count
                    result.append(f"{path.name}: ประชากรจำแนกกลุ่ม {count} แถว")
                except Exception as exc:
                    failed_count = total_rows
                    record_import_error(connection, import_id, None, str(exc))
                    result.append(f"{path.name}: นำเข้าไม่สำเร็จ - {exc}")
            elif {"intersection_id", "intersection_name", "vehicle_volume"}.issubset(headers):
                try:
                    count = import_zone_traffic(path, rows, connection)
                    success_count = count
                    result.append(f"{path.name}: ปริมาณรถรายแยก {count} แถว")
                except Exception as exc:
                    failed_count = total_rows
                    record_import_error(connection, import_id, None, str(exc))
                    result.append(f"{path.name}: นำเข้าไม่สำเร็จ - {exc}")
            elif {"category_type", "category_label"}.issubset(headers) and any(h.startswith("pop_") for h in headers):
                try:
                    count = import_demographics_wide(path, rows, connection)
                    success_count = count
                    result.append(f"{path.name}: ประชากรจำแนกกลุ่ม (รูปแบบตาราง) {count} แถว")
                except Exception as exc:
                    failed_count = total_rows
                    record_import_error(connection, import_id, None, str(exc))
                    result.append(f"{path.name}: นำเข้าไม่สำเร็จ - {exc}")
            else:
                failed_count = total_rows
                record_import_error(connection, import_id, None, f"รูปแบบไฟล์ไม่รองรับ: {path.name}")
                result.append(f"{path.name}: รูปแบบไฟล์ไม่รองรับ")
            # update import summary per file
            update_import_record(connection, import_id, success_count, failed_count, None)
            connection.commit()
        connection.commit()
    finally:
        connection.close()
    print("\n".join(result))


if __name__ == "__main__":
    main()
