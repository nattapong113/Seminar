from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Callable, Optional

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import connect, initialize_database

Importer = Callable[[Path, list[dict[str, str]], psycopg.Connection], int]


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


def import_monthly(path: Path, rows: list[dict[str, str]], connection: psycopg.Connection) -> int:
    location = "พัทยา ชลบุรี"
    connection.execute("DELETE FROM tourism_monthly WHERE source_file = %s", (path.name,))
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
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO tourism_monthly
                (source_file, location, calendar_year_be, calendar_year, month_id,
                 month_name, number_of_tourists, unit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            values,
        )
    return len(values)


def import_demographics(path: Path, rows: list[dict[str, str]], connection: psycopg.Connection) -> int:
    location = "พัทยา ชลบุรี"
    connection.execute("DELETE FROM demographic_profiles WHERE source_file = %s", (path.name,))
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
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO demographic_profiles
                (source_file, location, calendar_year_be, calendar_year, category,
                 segment, population, unit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            values,
        )
    return len(values)


def import_zone_traffic(path: Path, rows: list[dict[str, str]], connection: psycopg.Connection) -> int:
    connection.execute("DELETE FROM zone_traffic_volume WHERE source_file = %s", (path.name,))
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
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO zone_traffic_volume
                (source_file, intersection_id, intersection_name, calendar_year_be, calendar_year,
                 month_id, month_name, vehicle_volume, unit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            values,
        )
    return len(values)


def import_demographics_wide(path: Path, rows: list[dict[str, str]], connection: psycopg.Connection) -> int:
    """รองรับไฟล์รูปแบบจริงของเทศบาลเมืองพัทยา: category_type,category_label,pop_2563,pop_2564,..."""
    location = "พัทยา ชลบุรี"
    connection.execute("DELETE FROM demographic_profiles WHERE source_file = %s", (path.name,))
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
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO demographic_profiles
                (source_file, location, calendar_year_be, calendar_year, category,
                 segment, population, unit)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            values,
        )
    return len(values)


def detect_importer(headers: set[str]) -> Optional[tuple[Importer, str]]:
    """เลือกตัวนำเข้าจากหัวคอลัมน์ของไฟล์ คืน (ฟังก์ชัน, คำอธิบาย) หรือ None ถ้าไม่รู้จักรูปแบบ"""
    if {"calendar_year", "month_id", "number_of_tourists"}.issubset(headers):
        return import_monthly, "นักท่องเที่ยวรายเดือน"
    if {"year", "population"}.issubset(headers):
        return import_demographics, "ประชากรจำแนกกลุ่ม"
    if {"intersection_id", "intersection_name", "vehicle_volume"}.issubset(headers):
        return import_zone_traffic, "ปริมาณรถรายแยก"
    if {"category_type", "category_label"}.issubset(headers) and any(h.startswith("pop_") for h in headers):
        return import_demographics_wide, "ประชากรจำแนกกลุ่ม (รูปแบบตาราง)"
    return None


def ensure_data_source(connection: psycopg.Connection, name: str, source_type: str, endpoint: Optional[str] = None, metadata: Optional[str] = None) -> int:
    row = connection.execute("SELECT id FROM data_sources WHERE name = %s", (name,)).fetchone()
    if row:
        return row["id"]
    return connection.execute(
        "INSERT INTO data_sources (name, source_type, endpoint, metadata) VALUES (%s, %s, %s, %s) RETURNING id",
        (name, source_type, endpoint, metadata),
    ).fetchone()["id"]


def create_import_record(connection: psycopg.Connection, data_source_id: Optional[int], import_type: str, source_name: str, raw_filename: str, source_url: Optional[str], records_total: int) -> int:
    return connection.execute(
        "INSERT INTO imports (data_source_id, import_type, source_name, raw_filename, source_url, records_total, records_success, records_failed) VALUES (%s, %s, %s, %s, %s, %s, 0, 0) RETURNING id",
        (data_source_id, import_type, source_name, raw_filename, source_url, records_total),
    ).fetchone()["id"]


def update_import_record(connection: psycopg.Connection, import_id: int, success: int, failed: int, notes: Optional[str] = None) -> None:
    connection.execute(
        "UPDATE imports SET records_success = %s, records_failed = %s, notes = %s WHERE id = %s",
        (success, failed, notes, import_id),
    )


def record_import_error(connection: psycopg.Connection, import_id: int, row_number: Optional[int], error_text: str) -> None:
    connection.execute(
        "INSERT INTO import_errors (import_id, row_number, error_text) VALUES (%s, %s, %s)",
        (import_id, row_number, error_text),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="นำเข้า CSV ข้อมูลท่องเที่ยวและประชากร")
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.files:
        if not path.exists():
            raise SystemExit(f"ไม่พบไฟล์: {path}")
    initialize_database()
    result: list[str] = []
    with connect() as connection:
        for path in args.files:
            rows = read_csv(path)
            total_rows = len(rows)
            # register data source and import record
            data_source_id = ensure_data_source(connection, path.stem, "csv")
            import_id = create_import_record(connection, data_source_id, "csv", path.name, path.name, None, total_rows)
            success_count = 0
            failed_count = 0
            detected = detect_importer(set(rows[0]) if rows else set())
            if detected is None:
                failed_count = total_rows
                record_import_error(connection, import_id, None, f"รูปแบบไฟล์ไม่รองรับ: {path.name}")
                result.append(f"{path.name}: รูปแบบไฟล์ไม่รองรับ")
            else:
                importer, label = detected
                try:
                    # savepoint: ถ้านำเข้าพังกลางทาง ข้อมูลเดิมของไฟล์นี้ที่เพิ่งถูก DELETE จะถูกคืนกลับมาครบ
                    # แทนที่จะหายไปหรือเหลือครึ่ง ๆ กลาง ๆ
                    with connection.transaction():
                        success_count = importer(path, rows, connection)
                    result.append(f"{path.name}: {label} {success_count} แถว")
                except Exception as exc:
                    failed_count = total_rows
                    record_import_error(connection, import_id, None, str(exc))
                    result.append(f"{path.name}: นำเข้าไม่สำเร็จ - {exc}")
            # update import summary per file
            update_import_record(connection, import_id, success_count, failed_count, None)
            connection.commit()
    print("\n".join(result))


if __name__ == "__main__":
    main()
