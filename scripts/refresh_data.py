"""อัปเดตชุดข้อมูลที่ดึงจาก API ภายนอกให้เป็นปัจจุบัน

แต่ละงานมีรอบของตัวเอง และดูจากเวลานำเข้าล่าสุดในตาราง imports ว่าถึงรอบหรือยัง จึงไม่ต้องเก็บสถานะแยก
รันซ้ำจากหลายที่ได้ (เซิร์ฟเวอร์เว็บ, cron, GitHub Actions) โดยไม่ดึงข้อมูลซ้ำก่อนถึงรอบ
ไฟล์ CSV ของเมืองพัทยาไม่อยู่ในนี้ เพราะต้องดาวน์โหลดเอง ให้นำเข้าผ่านหน้าเว็บหรือ scripts/import_csv.py
"""
from __future__ import annotations

import argparse
import sys
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import connect, initialize_database
from scripts import import_holidays, import_poi_osm, import_weather_history


@dataclass(frozen=True)
class Job:
    label: str
    source_name: str
    interval_hours: int
    # แยกดึงกับเขียนออกจากกัน จะได้ไม่มี transaction ค้างอยู่บน Supabase ระหว่างรอ API ภายนอก
    fetch: Callable[[], Any]
    store: Callable[[psycopg.Connection, Any], int]


def fetch_weather() -> dict:
    with connect() as connection:
        start = import_weather_history.default_start(connection)
    # ดึงทั้งช่วงทุกครั้ง ERA5 ปรับแก้ค่าของวันล่าสุดย้อนหลังได้ และยอดระเบียนของการนำเข้าล่าสุด
    # ที่แสดงบนแดชบอร์ดจะได้เป็นยอดของทั้งชุดข้อมูล ไม่ใช่แค่วันที่เพิ่มมา
    return import_weather_history.fetch_weather(start, date.today() - timedelta(days=1))


def store_weather(connection: psycopg.Connection, payload: dict) -> int:
    return import_weather_history.import_weather(connection, payload)[0]


def fetch_holidays() -> list[dict]:
    # ไม่สลับไปแหล่งสำรองเองเหมือนตอนรันสคริปต์ด้วยมือ แหล่งสำรองตั้งชื่อวันหยุดต่างกัน
    # ถ้าแหล่งหลักล่ม ให้ข้ามรอบนี้แล้วลองใหม่รอบหน้า ดีกว่าให้ชื่อวันหยุดบนแดชบอร์ดเปลี่ยนไปมา
    records = import_holidays.fetch_gcal()
    if not records:
        raise ValueError("ต้นทางไม่ส่งรายการวันหยุดมา")
    return records


def store_holidays(connection: psycopg.Connection, records: list[dict]) -> int:
    return import_holidays.import_holidays(
        connection, import_holidays.GCAL_NAME, import_holidays.GCAL_URL, records, replace=True
    )[0]


def store_poi(connection: psycopg.Connection, payload: dict) -> int:
    return import_poi_osm.import_poi(connection, payload)[0]


# รอบตามความถี่ที่ต้นทางเปลี่ยนจริง Overpass เป็นเซิร์ฟเวอร์สาธารณะจึงเรียกให้น้อยที่สุด
JOBS: dict[str, Job] = {
    "weather": Job("สภาพอากาศรายวันย้อนหลัง", import_weather_history.SOURCE_NAME, 24, fetch_weather, store_weather),
    "holidays": Job("วันหยุดและเทศกาล", import_holidays.GCAL_NAME, 24 * 7, fetch_holidays, store_holidays),
    "poi": Job("สถานที่ท่องเที่ยว/ธุรกิจ", import_poi_osm.SOURCE_NAME, 24 * 30, import_poi_osm.fetch_poi, store_poi),
}
SOURCE_NAMES = [job.source_name for job in JOBS.values()]

LAST_RUN_SQL = """
SELECT DISTINCT ON (source_name) source_name, records_success,
       to_char(imported_at AT TIME ZONE 'Asia/Bangkok', 'YYYY-MM-DD HH24:MI') AS last_run,
       EXTRACT(EPOCH FROM now() - imported_at) / 3600 AS hours_ago
FROM imports WHERE source_name = ANY(%s)
ORDER BY source_name, imported_at DESC, id DESC
"""

# ข้อผิดพลาดของการรันครั้งล่าสุดของแต่ละงาน เก็บในหน่วยความจำของโปรเซส หายเมื่อรันสำเร็จหรือเปิดเซิร์ฟเวอร์ใหม่
_errors: dict[str, str] = {}
# กันรอบอัตโนมัติกับปุ่มสั่งเองบนหน้าเว็บดึงข้อมูลชุดเดียวกันพร้อมกัน แยกล็อกต่องาน
# งานที่ช้า (Overpass อาจรอเป็นนาที) จะได้ไม่บล็อกงานอื่น
_locks = {key: threading.Lock() for key in JOBS}


def status(last_runs: Iterable[dict]) -> list[dict]:
    """สถานะของทุกงาน รับผลของ LAST_RUN_SQL เข้ามา เพื่อให้เว็บใช้ connection pool ของตัวเองได้"""
    by_source = {row["source_name"]: row for row in last_runs}
    result = []
    for key, job in JOBS.items():
        last = by_source.get(job.source_name)
        result.append({
            "key": key,
            "label": job.label,
            "source_name": job.source_name,
            "interval_hours": job.interval_hours,
            "last_run": last["last_run"] if last else None,
            "records": last["records_success"] if last else None,
            "due": last is None or float(last["hours_ago"]) >= job.interval_hours,
            "error": _errors.get(key),
        })
    return result


def run(keys: Optional[Iterable[str]] = None, force: bool = False) -> Iterator[dict]:
    """รันงานที่ถึงรอบ (หรือทุกงานที่ระบุถ้า force) งานหนึ่งพังไม่ทำให้งานอื่นไม่ได้รัน

    คืนผลทีละงานทันทีที่เสร็จ ผู้เรียกจะได้บันทึกผลของงานที่เสร็จแล้วโดยไม่ต้องรองานที่ช้า
    """
    with connect() as connection:
        current = {item["key"]: item for item in status(connection.execute(LAST_RUN_SQL, (SOURCE_NAMES,)).fetchall())}
    for key in keys or JOBS:
        job = JOBS[key]
        if not force and not current[key]["due"]:
            continue
        outcome = {"key": key, "label": job.label}
        with _locks[key]:
            try:
                payload = job.fetch()
                with connect() as connection:
                    outcome.update(ok=True, records=job.store(connection, payload))
                _errors.pop(key, None)
            except Exception as exc:
                # first_failure ให้ผู้เรียกบันทึกประวัติเฉพาะครั้งแรกที่เริ่มพัง ไม่ใช่ทุกรอบที่ลองใหม่
                outcome.update(ok=False, error=str(exc), first_failure=key not in _errors)
                _errors[key] = str(exc)
        yield outcome


def main() -> None:
    parser = argparse.ArgumentParser(description="อัปเดตข้อมูลจาก API ภายนอกที่ถึงรอบ (สภาพอากาศ วันหยุด สถานที่)")
    parser.add_argument("--only", nargs="*", choices=sorted(JOBS), default=None, help="รันเฉพาะงานที่ระบุ (ค่าเริ่มต้น: ทุกงาน)")
    parser.add_argument("--force", action="store_true", help="รันแม้ยังไม่ถึงรอบ")
    args = parser.parse_args()

    initialize_database()
    results = list(run(args.only, force=args.force))
    if not results:
        print("ยังไม่มีงานที่ถึงรอบ (ใส่ --force เพื่อรันทันที)")
    for outcome in results:
        if outcome["ok"]:
            print(f"{outcome['label']}: อัปเดตสำเร็จ {outcome['records']} ระเบียน")
        else:
            print(f"{outcome['label']}: ไม่สำเร็จ - {outcome['error']}")
    if any(not outcome["ok"] for outcome in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
