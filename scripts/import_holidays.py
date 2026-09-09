from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import get_connection, initialize_database

SOURCE_NAME = "World Holidays API (TH)"
SOURCE_URL = "https://beomq.github.io/world_holidays/api/holidays/th.json"


def fetch_holidays() -> dict:
    request = Request(SOURCE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def ensure_data_source(connection: sqlite3.Connection, name: str, source_type: str, endpoint: Optional[str] = None) -> int:
    row = connection.execute("SELECT id FROM data_sources WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    cursor = connection.execute(
        "INSERT INTO data_sources (name, source_type, endpoint) VALUES (?, ?, ?)",
        (name, source_type, endpoint),
    )
    return cursor.lastrowid


def import_holidays(connection: sqlite3.Connection, payload: dict, years: Optional[set[int]]) -> tuple[int, int]:
    data_source_id = ensure_data_source(connection, SOURCE_NAME, "open_api", SOURCE_URL)
    holidays = payload.get("holidays", [])
    if years:
        holidays = [item for item in holidays if int(item["date"][:4]) in years]

    import_cursor = connection.execute(
        """
        INSERT INTO imports (data_source_id, import_type, source_name, source_url, records_total, records_success, records_failed)
        VALUES (?, 'api', ?, ?, ?, 0, 0)
        """,
        (data_source_id, SOURCE_NAME, SOURCE_URL, len(holidays)),
    )
    import_id = import_cursor.lastrowid

    success = 0
    failed = 0
    for item in holidays:
        try:
            connection.execute(
                """
                INSERT INTO public_holidays (data_source_id, date, name, local_name, holiday_type, country_code)
                VALUES (?, ?, ?, ?, ?, 'TH')
                ON CONFLICT(date, name, country_code) DO UPDATE SET
                    holiday_type = excluded.holiday_type,
                    local_name = excluded.local_name
                """,
                (
                    data_source_id,
                    item["date"],
                    item["name"],
                    (item.get("description") or {}).get("en") or item["name"],
                    item.get("type"),
                ),
            )
            success += 1
        except Exception as exc:  # malformed record from the feed
            failed += 1
            connection.execute(
                "INSERT INTO import_errors (import_id, error_text) VALUES (?, ?)",
                (import_id, f"{item}: {exc}"),
            )

    connection.execute(
        "UPDATE imports SET records_success = ?, records_failed = ? WHERE id = ?",
        (success, failed, import_id),
    )
    return success, failed


def main() -> None:
    parser = argparse.ArgumentParser(description="นำเข้าวันหยุดและเทศกาลของไทยจาก Open Data")
    parser.add_argument("--years", nargs="*", type=int, default=None, help="ปี ค.ศ. ที่ต้องการ เช่น 2025 2026 (ค่าเริ่มต้น: ทั้งหมดที่มี)")
    args = parser.parse_args()

    try:
        payload = fetch_holidays()
    except (URLError, TimeoutError) as exc:
        raise SystemExit(f"ดึงข้อมูลวันหยุดไม่สำเร็จ: {exc}")

    initialize_database()
    connection = get_connection()
    try:
        success, failed = import_holidays(connection, payload, set(args.years) if args.years else None)
        connection.commit()
    finally:
        connection.close()
    print(f"นำเข้าวันหยุด/เทศกาลสำเร็จ {success} รายการ (ผิดพลาด {failed}) จาก {SOURCE_URL}")


if __name__ == "__main__":
    main()
