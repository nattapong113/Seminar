"""นำเข้าสภาพอากาศรายวันย้อนหลังของพัทยาจาก Open-Meteo Archive API (ข้อมูล ERA5 ไม่ต้องใช้ API key)

ใช้เป็นฐานของการวิเคราะห์ผลกระทบของฝนใน /api/impact ต่างจาก /api/weather/* ที่เรียกสดและไม่เก็บลงฐานข้อมูล
ค่าเริ่มต้นดึงตั้งแต่เดือนแรกที่มีข้อมูลนักท่องเที่ยว จนถึงเมื่อวาน (ERA5 ตามหลังปัจจุบันประมาณ 2-5 วัน)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import connect, initialize_database

SOURCE_NAME = "Open-Meteo Archive (ERA5)"
ENDPOINT = "https://archive-api.open-meteo.com/v1/archive"
PATTAYA_LATITUDE = 12.9236
PATTAYA_LONGITUDE = 100.8694
DAILY_FIELDS = "precipitation_sum,precipitation_hours,temperature_2m_max,temperature_2m_min"


def fetch_weather(start: date, end: date) -> dict:
    parameters = urlencode({
        "latitude": PATTAYA_LATITUDE,
        "longitude": PATTAYA_LONGITUDE,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": DAILY_FIELDS,
        "timezone": "Asia/Bangkok",
    })
    request = Request(f"{ENDPOINT}?{parameters}", headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=120) as response:
        return json.load(response)


def ensure_data_source(connection: psycopg.Connection, name: str, source_type: str, endpoint: Optional[str] = None) -> int:
    row = connection.execute("SELECT id FROM data_sources WHERE name = %s", (name,)).fetchone()
    if row:
        return row["id"]
    return connection.execute(
        "INSERT INTO data_sources (name, source_type, endpoint) VALUES (%s, %s, %s) RETURNING id",
        (name, source_type, endpoint),
    ).fetchone()["id"]


def default_start(connection: psycopg.Connection) -> date:
    """เริ่มจากเดือนแรกที่มีข้อมูลนักท่องเที่ยว เพื่อให้จับคู่ฝนกับจำนวนคนได้ครบทุกเดือน"""
    row = connection.execute(
        "SELECT MIN(calendar_year) AS year FROM tourism_monthly"
    ).fetchone()
    return date(row["year"], 1, 1) if row and row["year"] else date.today().replace(month=1, day=1)


def import_weather(connection: psycopg.Connection, payload: dict) -> tuple[int, int]:
    data_source_id = ensure_data_source(connection, SOURCE_NAME, "open_api", ENDPOINT)
    daily = payload.get("daily", {})
    days = daily.get("time", [])

    import_id = connection.execute(
        """
        INSERT INTO imports (data_source_id, import_type, source_name, source_url, records_total, records_success, records_failed)
        VALUES (%s, 'api', %s, %s, %s, 0, 0)
        RETURNING id
        """,
        (data_source_id, SOURCE_NAME, ENDPOINT, len(days)),
    ).fetchone()["id"]

    # ตรวจแต่ละวันใน Python ก่อนแล้วเขียนรวดเดียว ฐานข้อมูลอยู่บนคลาวด์ ถ้าเขียนทีละแถวจะช้ามาก
    values = []
    errors = []
    for index, day in enumerate(days):
        try:
            values.append((
                data_source_id,
                date.fromisoformat(day),
                daily["precipitation_sum"][index],
                daily["precipitation_hours"][index],
                daily["temperature_2m_max"][index],
                daily["temperature_2m_min"][index],
            ))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            errors.append(f"{day}: {exc}")

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO weather_daily
                (data_source_id, date, precipitation_mm, precipitation_hours, temperature_max_c, temperature_min_c)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(date) DO UPDATE SET
                precipitation_mm = excluded.precipitation_mm,
                precipitation_hours = excluded.precipitation_hours,
                temperature_max_c = excluded.temperature_max_c,
                temperature_min_c = excluded.temperature_min_c,
                imported_at = now()
            """,
            values,
        )
        cursor.executemany(
            "INSERT INTO import_errors (import_id, error_text) VALUES (%s, %s)",
            [(import_id, error) for error in errors],
        )

    connection.execute(
        "UPDATE imports SET records_success = %s, records_failed = %s WHERE id = %s",
        (len(values), len(errors), import_id),
    )
    return len(values), len(errors)


def main() -> None:
    parser = argparse.ArgumentParser(description="นำเข้าสภาพอากาศรายวันย้อนหลังของพัทยาจาก Open-Meteo Archive")
    parser.add_argument("--start", type=date.fromisoformat, default=None, help="วันเริ่ม เช่น 2022-01-01 (ค่าเริ่มต้น: ปีแรกที่มีข้อมูลนักท่องเที่ยว)")
    parser.add_argument("--end", type=date.fromisoformat, default=date.today() - timedelta(days=1), help="วันสุดท้าย (ค่าเริ่มต้น: เมื่อวาน)")
    args = parser.parse_args()

    initialize_database()
    with connect() as connection:
        start = args.start or default_start(connection)
        if start > args.end:
            raise SystemExit(f"ช่วงวันที่ไม่ถูกต้อง: {start} ถึง {args.end}")
        try:
            payload = fetch_weather(start, args.end)
        except (URLError, TimeoutError) as exc:
            raise SystemExit(f"ดึงข้อมูลสภาพอากาศย้อนหลังไม่สำเร็จ: {exc}")
        success, failed = import_weather(connection, payload)
    print(f"นำเข้าสภาพอากาศรายวัน {start} ถึง {args.end} สำเร็จ {success} วัน (ผิดพลาด {failed}) จาก {SOURCE_NAME}")


if __name__ == "__main__":
    main()
