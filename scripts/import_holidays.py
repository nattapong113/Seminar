from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import connect, initialize_database

SOURCE_NAME = "World Holidays API (TH)"
SOURCE_URL = "https://beomq.github.io/world_holidays/api/holidays/th.json"


# แหล่งข้อมูลให้ชื่อวันหยุดเป็นภาษาอังกฤษและเกาหลีเท่านั้น (ไม่มีไทย) จึงต้องแปลงเป็นชื่อราชการไทยเอง
# คีย์คือชื่อหลักหลังตัดคำต่อท้ายที่บอกว่าเป็นวันชดเชยออกแล้ว (ดูฟังก์ชัน thai_name)
THAI_NAMES: dict[str, str] = {
    "New Year's Day": "วันขึ้นปีใหม่",
    "New Year's Eve": "วันสิ้นปี",
    "New Year Special Holiday": "วันหยุดพิเศษช่วงปีใหม่",
    "National Children's Day": "วันเด็กแห่งชาติ",
    "Makha Bucha": "วันมาฆบูชา",
    "Chakri Day": "วันจักรี",
    "Chakri Memorial Day": "วันจักรี",
    "Songkran": "วันสงกรานต์",
    "Songkran Festival": "เทศกาลสงกรานต์",
    "Songkran Holiday": "วันหยุดเทศกาลสงกรานต์",
    "Songkran Observed": "วันสงกรานต์ (ชดเชย)",
    "National Labor Day": "วันแรงงานแห่งชาติ",
    "Coronation Day": "วันฉัตรมงคล",
    "Royal Ploughing Ceremony Day": "วันพืชมงคล",
    "Visakha Bucha": "วันวิสาขบูชา",
    "Queen Suthida's Birthday": "วันเฉลิมพระชนมพรรษาสมเด็จพระนางเจ้าสุทิดาฯ",
    "HM Queen Suthida's Birthday": "วันเฉลิมพระชนมพรรษาสมเด็จพระนางเจ้าสุทิดาฯ",
    "Asalha Bucha": "วันอาสาฬหบูชา",
    "Asarnha Bucha": "วันอาสาฬหบูชา",
    "Buddhist Lent Day": "วันเข้าพรรษา",
    "King Vajiralongkorn's Birthday": "วันเฉลิมพระชนมพรรษา รัชกาลที่ 10",
    "HM King Maha Vajiralongkorn's Birthday": "วันเฉลิมพระชนมพรรษา รัชกาลที่ 10",
    "The Queen Mother's Birthday": "วันแม่แห่งชาติ",
    "HM Queen Sirikit The Queen Mother's Birthday; National Mother's Day": "วันเฉลิมพระชนมพรรษาสมเด็จพระบรมราชชนนีพันปีหลวง / วันแม่แห่งชาติ",
    "Anniversary of the Death of King Bhumibol": "วันคล้ายวันสวรรคต รัชกาลที่ 9",
    "HM King Bhumibol Adulyadej Memorial Day": "วันคล้ายวันสวรรคต รัชกาลที่ 9",
    "Chulalongkorn Day": "วันปิยมหาราช",
    "HM King Chulalongkorn Memorial Day": "วันปิยมหาราช",
    "King Bhumibol's Birthday/Father's Day": "วันคล้ายวันพระบรมราชสมภพ รัชกาลที่ 9 / วันพ่อแห่งชาติ",
    "HM King Bhumibol Adulyadej the Great's Birthday; National Day; National Father's Day": "วันคล้ายวันพระบรมราชสมภพ รัชกาลที่ 9 / วันชาติ / วันพ่อแห่งชาติ",
    "Constitution Day": "วันรัฐธรรมนูญ",
    "Bridge Public Holiday": "วันหยุดพิเศษเชื่อมวันหยุดยาว",
}

# คำต่อท้ายที่แหล่งข้อมูลใช้บอกว่าเป็นวันหยุดชดเชย
OBSERVED_SUFFIXES = (" (Observed)", " (in lieu)")


def thai_name(english_name: str) -> str:
    """แปลงชื่อวันหยุดภาษาอังกฤษเป็นชื่อไทย คงคำว่า (ชดเชย) ไว้ถ้าต้นทางระบุ

    ต้นทางเขียนวันชดเชยได้สองแบบ คือต่อท้ายทั้งชื่อ ("Chakri Day (Observed)") และต่อท้ายทุกท่อน
    ของชื่อที่รวมหลายวันไว้ด้วยกัน ("A (in lieu); B (in lieu)") จึงตัดคำต่อท้ายทีละท่อน
    """
    observed = False
    parts: list[str] = []
    for part in english_name.split("; "):
        part = part.strip()
        for suffix in OBSERVED_SUFFIXES:
            if part.endswith(suffix):
                part = part[: -len(suffix)].strip()
                observed = True
        parts.append(part)
    translated = THAI_NAMES.get("; ".join(parts))
    if not translated:
        return english_name
    return f"{translated} (ชดเชย)" if observed else translated


def fetch_holidays() -> dict:
    request = Request(SOURCE_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def ensure_data_source(connection: psycopg.Connection, name: str, source_type: str, endpoint: Optional[str] = None) -> int:
    row = connection.execute("SELECT id FROM data_sources WHERE name = %s", (name,)).fetchone()
    if row:
        return row["id"]
    return connection.execute(
        "INSERT INTO data_sources (name, source_type, endpoint) VALUES (%s, %s, %s) RETURNING id",
        (name, source_type, endpoint),
    ).fetchone()["id"]


def import_holidays(connection: psycopg.Connection, payload: dict, years: Optional[set[int]]) -> tuple[int, int]:
    data_source_id = ensure_data_source(connection, SOURCE_NAME, "open_api", SOURCE_URL)
    holidays = payload.get("holidays", [])
    if years:
        holidays = [item for item in holidays if int(item["date"][:4]) in years]

    import_id = connection.execute(
        """
        INSERT INTO imports (data_source_id, import_type, source_name, source_url, records_total, records_success, records_failed)
        VALUES (%s, 'api', %s, %s, %s, 0, 0)
        RETURNING id
        """,
        (data_source_id, SOURCE_NAME, SOURCE_URL, len(holidays)),
    ).fetchone()["id"]

    # ตรวจรายการจากต้นทางใน Python ก่อนแล้วเขียนรวดเดียว ฐานข้อมูลอยู่บนคลาวด์ ถ้าเขียนทีละแถวจะช้ามาก
    values = []
    errors = []
    for item in holidays:
        try:
            values.append((
                data_source_id,
                date.fromisoformat(item["date"]),
                item["name"],
                thai_name(item["name"]),
                item.get("type"),
            ))
        except (KeyError, TypeError, ValueError) as exc:  # malformed record from the feed
            errors.append(f"{item}: {exc}")

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO public_holidays (data_source_id, date, name, local_name, holiday_type, country_code)
            VALUES (%s, %s, %s, %s, %s, 'TH')
            ON CONFLICT(date, name, country_code) DO UPDATE SET
                holiday_type = excluded.holiday_type,
                local_name = excluded.local_name
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
    parser = argparse.ArgumentParser(description="นำเข้าวันหยุดและเทศกาลของไทยจาก Open Data")
    parser.add_argument("--years", nargs="*", type=int, default=None, help="ปี ค.ศ. ที่ต้องการ เช่น 2025 2026 (ค่าเริ่มต้น: ทั้งหมดที่มี)")
    args = parser.parse_args()

    try:
        payload = fetch_holidays()
    except (URLError, TimeoutError) as exc:
        raise SystemExit(f"ดึงข้อมูลวันหยุดไม่สำเร็จ: {exc}")

    initialize_database()
    with connect() as connection:
        success, failed = import_holidays(connection, payload, set(args.years) if args.years else None)
    print(f"นำเข้าวันหยุด/เทศกาลสำเร็จ {success} รายการ (ผิดพลาด {failed}) จาก {SOURCE_URL}")


if __name__ == "__main__":
    main()
