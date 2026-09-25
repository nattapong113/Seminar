from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Callable, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

import psycopg

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.database import connect, initialize_database

# ประเภทวันหยุดที่เก็บลงคอลัมน์ holiday_type
# public / observed คือวันที่ราชการหยุดจริง ใช้ในการวิเคราะห์ผลกระทบ
# observance คือวันสำคัญที่ไม่ใช่วันหยุดราชการ (ตรุษจีน คริสต์มาส วาเลนไทน์) ไม่นับเป็นวันหยุด
# แต่ยังเก็บไว้เพราะมีผลต่อการท่องเที่ยวของพัทยา
TYPE_PUBLIC = "public"
TYPE_OBSERVED = "observed"
TYPE_OBSERVANCE = "observance"
ANALYSIS_TYPES = (TYPE_PUBLIC, TYPE_OBSERVED)


# ---------------------------------------------------------------- แหล่งที่ 1: ปฏิทินวันหยุดไทย (iCalendar)

GCAL_NAME = "Thai Public Holidays Calendar (iCalendar)"
GCAL_URL = (
    "https://calendar.google.com/calendar/ical/"
    "th.th%23holiday%40group.v.calendar.google.com/public/basic.ics"
)

# ต้นทางแยกประเภทไว้ในฟิลด์ DESCRIPTION อยู่แล้ว จึงไม่ต้องเดาจากชื่อ
GCAL_PUBLIC_MARKER = "นักขัตฤกษ์"
# อักขระความกว้างศูนย์ที่ปนมาในชื่อบางรายการของต้นทาง ถ้าไม่ตัดออกจะทำให้ชื่อซ้ำกันแต่เทียบไม่ตรง
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍﻿"))

# คำสะกดผิดที่ติดมาจากต้นทางทุกปี แก้ตอนนำเข้าเพื่อไม่ให้ขึ้นผิดบนแดชบอร์ดและในรายงาน
GCAL_SPELLING_FIXES = (("ขื้นปีใหม่", "ขึ้นปีใหม่"),)


def fix_spelling(name: str) -> str:
    for wrong, right in GCAL_SPELLING_FIXES:
        name = name.replace(wrong, right)
    return name


def unfold_ics(text: str) -> str:
    """คืนบรรทัดที่ถูกตัดตามมาตรฐาน iCalendar (RFC 5545) ให้กลับมาเป็นบรรทัดเดียว

    ต้นทางตัดบรรทัดที่ความยาว 75 ไบต์แล้วขึ้นบรรทัดใหม่โดยเว้นวรรคนำหน้า
    ชื่อวันหยุดไทยยาวเกินเสมอ ถ้าไม่ต่อกลับจะได้ชื่อที่ขาดกลางคัน
    """
    return re.sub(r"\r?\n[ \t]", "", text)


def unescape_ics(value: str) -> str:
    """ถอดอักขระหนีของ iCalendar (\\, \\; \\n) กลับเป็นข้อความปกติ"""
    return (
        value.replace("\\n", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def parse_ics(text: str) -> list[dict]:
    records: list[dict] = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfold_ics(text), re.S):
        start = re.search(r"^DTSTART;VALUE=DATE:(\d{8})", block, re.M)
        summary = re.search(r"^SUMMARY:(.*)$", block, re.M)
        description = re.search(r"^DESCRIPTION:(.*)$", block, re.M)
        if not start or not summary:
            continue
        raw = start.group(1)
        name = fix_spelling(unescape_ics(summary.group(1)).translate(ZERO_WIDTH))
        is_public = GCAL_PUBLIC_MARKER in (description.group(1) if description else "")
        records.append({
            "date": f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}",
            "name": name,
            "local_name": name,
            # ต้นทางเรียกวันชดเชยว่า "วันหยุดชดเชย..." ตรงกันทุกรายการ
            "holiday_type": (TYPE_OBSERVED if name.startswith("วันหยุดชดเชย") else TYPE_PUBLIC)
            if is_public else TYPE_OBSERVANCE,
        })
    return records


def fetch_gcal() -> list[dict]:
    request = Request(GCAL_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        return parse_ics(response.read().decode("utf-8"))


# ---------------------------------------------------------------- แหล่งที่ 2: World Holidays API (สำรอง)

WORLD_NAME = "World Holidays API (TH)"
WORLD_URL = "https://beomq.github.io/world_holidays/api/holidays/th.json"

# แหล่งสำรองให้ชื่อวันหยุดเป็นภาษาอังกฤษเท่านั้น จึงต้องแปลงเป็นชื่อราชการไทยเอง
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


def thai_name(english_name: str) -> tuple[str, bool]:
    """แปลงชื่อวันหยุดภาษาอังกฤษเป็นชื่อไทย คืน (ชื่อไทย, เป็นวันชดเชยหรือไม่)

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
        return english_name, observed
    return (f"{translated} (ชดเชย)" if observed else translated), observed


def fetch_world() -> list[dict]:
    request = Request(WORLD_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    records: list[dict] = []
    for item in payload.get("holidays", []):
        local, observed = thai_name(item["name"])
        records.append({
            "date": item["date"],
            "name": item["name"],
            "local_name": local,
            "holiday_type": TYPE_OBSERVED if observed else TYPE_PUBLIC,
        })
    return records


PROVIDERS: dict[str, tuple[str, str, Callable[[], list[dict]]]] = {
    "calendar": (GCAL_NAME, GCAL_URL, fetch_gcal),
    "world": (WORLD_NAME, WORLD_URL, fetch_world),
}


# ---------------------------------------------------------------- นำเข้า


def ensure_data_source(connection: psycopg.Connection, name: str, source_type: str, endpoint: Optional[str] = None) -> int:
    row = connection.execute("SELECT id FROM data_sources WHERE name = %s", (name,)).fetchone()
    if row:
        return row["id"]
    return connection.execute(
        "INSERT INTO data_sources (name, source_type, endpoint) VALUES (%s, %s, %s) RETURNING id",
        (name, source_type, endpoint),
    ).fetchone()["id"]


def import_holidays(
    connection: psycopg.Connection,
    source_name: str,
    source_url: str,
    records: list[dict],
    replace: bool,
) -> tuple[int, int, int]:
    data_source_id = ensure_data_source(connection, source_name, "open_api", source_url)
    import_id = connection.execute(
        """
        INSERT INTO imports (data_source_id, import_type, source_name, source_url, records_total, records_success, records_failed)
        VALUES (%s, 'api', %s, %s, %s, 0, 0)
        RETURNING id
        """,
        (data_source_id, source_name, source_url, len(records)),
    ).fetchone()["id"]

    # ตรวจรายการจากต้นทางใน Python ก่อนแล้วเขียนรวดเดียว ฐานข้อมูลอยู่บนคลาวด์ ถ้าเขียนทีละแถวจะช้ามาก
    values = []
    errors = []
    for item in records:
        try:
            values.append((
                data_source_id,
                date.fromisoformat(item["date"]),
                item["name"],
                item["local_name"],
                item["holiday_type"],
            ))
        except (KeyError, TypeError, ValueError) as exc:  # รายการที่ต้นทางส่งมาไม่ครบ
            errors.append(f"{item}: {exc}")

    # คนละแหล่งตั้งชื่อวันเดียวกันไม่เหมือนกัน ถ้า upsert เฉย ๆ วันหยุดวันเดิมจะกลายเป็นสองแถว
    # แล้วการนับ "จำนวนวันหยุดต่อเดือน" จะเกินจริง จึงล้างช่วงวันที่ที่กำลังนำเข้าทิ้งก่อน
    removed = 0
    if replace and values:
        dates = [row[1] for row in values]
        removed = connection.execute(
            "DELETE FROM public_holidays WHERE country_code = 'TH' AND date BETWEEN %s AND %s",
            (min(dates), max(dates)),
        ).rowcount

    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO public_holidays (data_source_id, date, name, local_name, holiday_type, country_code)
            VALUES (%s, %s, %s, %s, %s, 'TH')
            ON CONFLICT(date, name, country_code) DO UPDATE SET
                holiday_type = excluded.holiday_type,
                local_name = excluded.local_name,
                data_source_id = excluded.data_source_id
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
    return len(values), len(errors), removed


def main() -> None:
    parser = argparse.ArgumentParser(description="นำเข้าวันหยุดและเทศกาลของไทยจาก Open Data")
    parser.add_argument("--years", nargs="*", type=int, default=None, help="ปี ค.ศ. ที่ต้องการ เช่น 2025 2026 (ค่าเริ่มต้น: ทั้งหมดที่มี)")
    parser.add_argument("--source", choices=sorted(PROVIDERS), default="calendar", help="แหล่งข้อมูล (ค่าเริ่มต้น: calendar)")
    parser.add_argument("--no-observances", action="store_true", help="ไม่นำเข้าวันสำคัญที่ไม่ใช่วันหยุดราชการ เช่น ตรุษจีน คริสต์มาส")
    parser.add_argument("--keep-existing", action="store_true", help="ไม่ลบวันหยุดเดิมในช่วงวันที่ที่นำเข้า (เสี่ยงนับซ้ำถ้าสลับแหล่ง)")
    args = parser.parse_args()

    order = [args.source] + [key for key in PROVIDERS if key != args.source]
    records: list[dict] = []
    source_name = source_url = ""
    for key in order:
        source_name, source_url, fetch = PROVIDERS[key]
        try:
            records = fetch()
            break
        except (URLError, TimeoutError, ValueError) as exc:
            print(f"ดึงจาก {source_name} ไม่สำเร็จ: {exc}")
    if not records:
        raise SystemExit("ดึงข้อมูลวันหยุดไม่สำเร็จจากทุกแหล่ง")

    if args.years:
        years = set(args.years)
        records = [item for item in records if int(item["date"][:4]) in years]
    if args.no_observances:
        records = [item for item in records if item["holiday_type"] != TYPE_OBSERVANCE]

    initialize_database()
    with connect() as connection:
        success, failed, removed = import_holidays(
            connection, source_name, source_url, records, replace=not args.keep_existing
        )

    public = sum(1 for item in records if item["holiday_type"] in ANALYSIS_TYPES)
    years_covered = sorted({item["date"][:4] for item in records})
    print(
        f"นำเข้าวันหยุด/วันสำคัญสำเร็จ {success} รายการ (วันหยุดราชการ {public} วัน, "
        f"ผิดพลาด {failed}, ลบของเดิมในช่วงเดียวกัน {removed}) "
        f"ครอบคลุมปี {years_covered[0]}-{years_covered[-1]} จาก {source_url}"
    )


if __name__ == "__main__":
    main()
